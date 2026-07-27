#!/bin/bash
#
# perstn-run.sh -- dispatch a specific PCIe bring-up RUN by letter or number.
#
# Usage:
#   ./Scripts/m1n1/perstn-run.sh {I|J|K|L|M|N|O|P|Q|R|S|1|2|3|4|5|6|7|8|9|10|11|12|13|14|15|16|17|18..31} [extra args...]
#
# RUNs 1-24 drive perstn.sh (PCIe bring-up). RUN 25+ drive soc_bringup.sh
# (SoC-first: SMP + companion-IOP recon). The RUNNER var selects which.
#
# Letters (A..S) are the historical RUN series (A-H were the pre-RUN-I
# scouting phase; I onward were single-hypothesis bisections). Numbers
# (1..N) begin a NEW series starting 2026-07-21 that iterates on top of
# RUN S's naked mask-RMW apply findings until PCIe trains. RUN 1 =
# RUN S + phycmn-early (FALSIFIED that CLK_MODE=ON is the co-factor).
# RUN 2 = RUN 1 + full cio3pllcore naked apply to axi_sub5_base
# (FALSIFIED -- all 7 entries SKIP-NOOP on sub5 because iBoot
# pre-programmed the block; wedge unchanged at phy_ip+0x38). RUN 3 =
# RUN 2 but redirects the naked cio3pllcore apply to phy_common_base
# per the atc.c CIO3PLL analogy in docs/ref-asahi-t8132-pcie.md
# (FALSIFIED -- phy_common +0x2a00..+0x2c00 all zero, cio3pllcore
# 1 PARTIAL / 5 NO-OP / 1 SKIP-NOOP; target-block brute-forcing
# retired). RUN 4 = RUN 1 baseline but replaces the DESTRUCTIVE
# --naked-write-test with a new READ-ONLY --naked-write-test-readonly
# to preserve iBoot's sub5+0 = 0x00081f55 PLL control word (which
# every RUN S/1/2/3 was clobbering to 0x00000a01). Retires the
# cio3pllcore search; tests whether the destructive probe has been
# the confounding variable for the last 5 RUNs (FALSIFIED --
# sub5+0 iBoot value preserved through post-5.75, pcieclkgen apply
# landed cleanly at 0x00081e35, yet phy_ip+0x38 wedge unchanged;
# destructive-probe hypothesis definitively ruled out). RUN 5 =
# RUN 4 baseline + --phy-ip-write-probe with --phy-ip-diag-at=
# post-7.phycmn-early. Tests whether phy_ip decodes POSTED writes
# even when reads AXI-stall, from the cleanest fabric state ever
# probed for write-decode (naked axi2af + naked pcieclkgen landed
# at sub5, CLK_MODE=ON, iBoot mostly preserved). RUN P tested this
# from a weaker broken-applicator baseline where no naked applies
# had landed; RUN 5 tests from the strong state (FALSIFIED --
# STALL branch: posted write32(phy_ip+0x38, 0) bus-hung m1n1
# identically to the read wedge; phy_ip is BIDIRECTIONALLY decode-
# locked from the RUN 4 baseline). RUN 6 = RUN 4 baseline +
# --t8122-shared-post. RUN 5's post-7.phycmn-early reachable-scan
# found phy_shared+0x8 = 0x00000000 (bit 0 CLEAR) and
# phy_shared+0 = 0xf3c0301f (bits 8, 9 CLEAR). pcie.c:543-551 has
# T8122/T602X/T6031 (but NOT T8140) run poll32(phy_shared+8, 1, 1,
# 250000) then set32(phy_shared+0, 0x200) [bit 9, T8122]. We SKIP
# the poll (would timeout) and do the set32 unconditionally.
# Hypothesis: bit 9 of phy_shared+0 is a phy_ip decode-enable gate
# T8140 doesn't have but T8132 needs (FALSIFIED -- bit 9 STUCK
# cleanly, phy_shared+0 = 0xf3c0321f, but 6.g wedges identically
# at phy_ip+0x38). RUN 7 = RUN 6 baseline but replaces the
# pcieclkgen mask-RMW (mask=0x3e0 val=0x220, which clears iBoot
# bits 6, 8) with --pcieclkgen-set5-only-to=axi_sub5_base
# (set32(sub5+0, 0x20) -- bit 5 only, preserving iBoot bits 6, 8).
# Hypothesis: bits 6, 8 are PLL enables that iBoot set and we've
# been clearing every RUN S/1/2/3/4/5/6, causing phy_ip to have
# no clock and AXI-stall on decode (FALSIFIED -- bit 5 landed
# STUCK at 0x00081f75 with iBoot bits 6, 8 preserved end-to-end,
# yet 6.g wedged identically at phy_ip+0x38). RUN 8 = RUN 7
# baseline + --phy4-x10-early (candidate C: T8122 pcie.c:529
# set32(phy_shared+4, 0x10), which T8140 skips, hoisted before
# the first phy_ip access) + read-only --pmgr-pre6g-scan
# (PS-register sweep of apcie/pcie/phy/auspma/cio-named PMGR
# devices just before 6.g) (FALSIFIED -- phy_shared+4 went
# 0x01 -> 0x11 STUCK, phy_shared+0x8 stayed 0, 6.g wedged
# identically; PMGR sweep showed every APCIE-family gate ON
# at the wedge point, weakening the power-domain hypothesis).
# RUN 9 = RUN 8 baseline + --t8122-shared-post-val=0x300
# (candidate D: T602X's pcie.c:549 set32(phy_shared+0, 0x300),
# bits 8+9 -- bit 8 has never been set on t8132) (FALSIFIED --
# bits 8+9 STUCK at 0xf3c0331f, 6.g wedged identically; the
# Python-replayable pcie.c write set is now EXHAUSTED). RUN 10 =
# RUN 9 baseline + --phy-ip-stub-apply=tail: on-CPU AArch64 stub
# (ARMAsm + p.call) re-runs the idempotent shared-init tail and
# applies the 29 pll tunable RMWs back-to-back with dsb sy
# barriers, microseconds apart -- tests the tight-timing-window
# hypothesis (proxy MMIO runs on the same CPU; only the seconds-
# long inter-access gaps differ from pcie.c) (FALSIFIED -- stub
# uploaded cleanly, p.call wedged with UartTimeout on the first
# phy_ip access). Git archaeology then reframed the blocker: on
# 2026-07-11 (d664bd9 era) p.pcie_init() RAN TO COMPLETION on
# this machine (per-port bring-up included, ports stuck at
# LINKSTS_BUSY) and Phase F's C-applicator 6.g then applied all
# 29 pll entries through phy_ip+0x38 SUCCESSFULLY -- phy_ip
# decodes after the C init's per-port bring-up, which the replay
# never runs. 6b277bc later moved the phy-ip tunables into the C
# path but wedges on the port-1 auspma slice (j773g has no
# pci-bridge1), hence --no-pcie-init and the whole replay series.
# RUN 11 = fix the C path: m1n1 patched with a t8132 port-slice
# filter (m1n1 commit b404263), full p.pcie_init() + tier-3
# post-init dumps incl. a phy_ip shared-window harvest
# (TOOLING BUG -- patched m1n1 confirmed enrolled (banner
# -dirty), but legacy Phase B ran alive for the first time in
# ~20 runs and its shared-MMIO sweep read phy_ip pre-init ->
# Exception: SYNC before p.pcie_init() ever ran; filter still
# untested). RUN 12 = RUN 11 minus --pmgr-enable/--pmgr-per-port
# (Phase B/C skipped; pcie.c:425 does its own power enable) with
# Phase B's phy_ip probes removed from perstn.py -- straight to
# the C-side pcie_init (WEDGED inside pcie_init: only
# "Initializing t8132" escaped the console buffer. Archaeology:
# pcie_up_2.log -- the FIRST test of 6b277bc -- shows the
# IDENTICAL signature, while pcie_up_1 (pre-6b277bc, no phy-ip
# tunables in C) returned 0. 6b277bc's phy-ip tunables at
# pcie.c:518 fire BEFORE per-port bring-up, where phy_ip never
# decodes on t8132 -- an ordering bug; the port-slice filter was
# necessary but not sufficient). RUN 13 = m1n1 7728fb0 SKIPS the
# phy-ip tunables in C on t8132 (restoring the pcie_up_1 rc=0
# behavior) + perstn.py applies them POST-init
# (--post-init-phy-ip: pll via C applicator reg_idx=3, the
# proven 2026-07-11 recipe; auspma Python-side with the
# port-slice filter), then tier-3 dumps + LTSSM kick + ECAM
# walk. pcie_init now runs with a 60 s UART timeout + 30 s
# post-timeout liveness recovery (STALE BINARY -- the 7728fb0
# build was staged but never enrolled; banner still
# 56-g6b277bc-dirty, no skip-printf anywhere; RUN 13 was a
# byte-identical re-run of RUN 12, no new hardware data).
# RUN 14 = RUN 13 done properly: m1n1 8a569ad (= 7728fb0 skip +
# PCIE_BC flushed breadcrumbs at every init stage, banner
# v1.6.0-rc1-59-g8a569ad) enrolled for real, enforced by the new
# --require-build=rc1-59-g guard (aborts pre-SMC on mismatch);
# --gate-poke dropped (Phase D was the one state-changing
# pre-init step the proven pcie_up_1 boot didn't have; pcie.c
# does its own pmgr enable) (BREAKTHROUGH -- p.pcie_init()
# returned 0 for the first time since 2026-07-10: full shared
# init + rc handshake OK; both ports "failed to become idle"
# (LINKSTS BUSY, continue'd) = pcie_up_1 parity per its BUSY
# post-init LINKSTS 0x8300020c/0x83000204, a NON-event; but the
# straight-to-pll post-init apply wedged -- phy_ip still locked
# right after pcie_init). RUN 15 = the 2026-07-11 recipe: on
# that boot phy_ip decoded only after pcie_init was followed by
# a FULL Phase F re-pass of the shared sequence (second
# CLK0/CLK1 handshake etc). --t8140-replay-post-init runs
# Phase F AFTER pcie_init (native order); its 6.g/6.h apply the
# phy-ip tunables python-filtered. No reflash needed (8a569ad
# stays enrolled) (TOOLING x2 -- Phase F post-init silently
# SKIPPED on its phaseD_gate151_active entry check (a python-
# side flag only --gate-poke sets; RUN 15 dropped it), then the
# unconditional Tier 3a phy_ip harvest wedged the boot; BONUS:
# the early tier-1 dump proved RUN 15's post-init state is
# register-identical to pcie_up_1's golden dump on all 7
# compared registers). RUN 16 = RUN 15 + post-init PMGR gate
# verification (sets the Phase F precondition from hardware
# truth via safe PS reads) + Tier 3a gated on phaseF_shared_up
# (FALSIFIED post-init+re-pass+per-entry: Phase F ran 1->6.f
# from the post-init state -- largely idempotent -- and 6.g
# wedged at entry #0 again). Archaeology then CORRECTED the
# 2026-07-11 record: c4c0b33's contemporary commit message
# proves 6.g completed via the C-APPLICATOR
# (p.tunables_apply_local reg_idx=3, the d664bd9 method), and
# the "pcie_init ran first" part was circular evidence --
# pcie_up_2 proved 6b277bc's pcie_init wedges, so the Jul-11
# boot almost certainly ran WITHOUT pcie_init. RUN 17 = the
# corrected reproduction: cold boot + Phase D + Phase F native
# order + --phyip-apply-local (6.g via C applicator; 6.h stays
# python-filtered). The cold+C-applicator matrix cell has never
# been retried (WEDGED identically at entry #0 -- AND the +55-
# byte post.6.g flush signature RUN 17 produced is
# indistinguishable from the Jul-11 record's, whose era's step()
# flushed the post marker on the exception path too. VERDICT:
# the Jul-11 "6.g success" was a misread wedge; phy_ip has NEVER
# decoded on this machine; the phy-ip tunables axis (37 boots)
# is CLOSED). RUN 18 pivots to the evidenced blocker: pcie_init
# returns 0, ports reach LTSSM BUSY (training STARTS) and never
# converge. Prime suspect: the CLKREQ# pin has been forced to a
# manual GPIO output LOW since the pcie_up era, overriding its
# ADT-declared alt-function 2 (the apcie controller manages the
# CLKREQ#/refclk handshake itself). RUN 18 = full pcie_init with
# --clkreq-mode=periph (restore the ADT pin function) + a 5 s
# post-init LINKSTS watch + dumps/kick/ECAM (no phy_ip anywhere;
# Tier 3a auto-gated off).
#
# Each RUN tests one specific hypothesis for what ungates phy_ip on
# t8132 (the current Phase F blocker). See docs/project-m4-pcie-
# bringup.md for the full state-of-the-art notes.
#
#   RUN I -- phy_common CLK_MODE=ON is the phy_ip ungate. Applies
#            it AFTER step 6.f (T8140 marker) and BEFORE any phy_ip
#            access. Single-variable vs RUN H.
#
#   RUN J -- apcie-cio3pllcore-tunables + apcie-pcieclkgen-tunables
#            supply the missing PCIe clock/PLL config that ungates
#            phy_ip. Applies both t8132-specific tunables after
#            step 5 and probes phy_ip at post-5.5.extra-tunables.
#            Single-variable vs RUN H.
#
#   RUN K -- RUN I + RUN J bundled. Only relevant if I and J both
#            fail alone -- tests whether their combination unblocks.
#            Probes at RUN I's checkpoint (later of the two).
#
#   RUN L -- set32(phy_shared+4, 0x10) (T8122's step 6.i, pcie.c:529)
#            moved from AFTER phy_ip tunables to BEFORE. Tests
#            whether this write, which T8140 skips entirely, is
#            the phy_ip ungate on t8132.
#
#   RUN M -- --extra-tunables restricted to apcie-cio3pllcore-tunables
#            only (7 writes to rc_base). Bisection of RUN J: which of
#            cio3pllcore or pcieclkgen flipped phy_ip's fault mode
#            from silent-AXI-stall to Exception: SYNC. If RUN M sees
#            the SYNC, cio3pllcore is the trigger (small-N follow-up).
#            If it silent-stalls, cio3pllcore is not the trigger --
#            proceed to RUN N.
#
#   RUN N -- --extra-tunables restricted to apcie-pcieclkgen-tunables
#            only (1 write, rc_base+0 mask 0x3e0 <- 0x220). Complement
#            of RUN M. Between RUNs M and N exactly one identifies
#            the fault-mode flipper (or both do, indicating either
#            alone is sufficient). Reuses --extra-tunables-only from
#            the RUN M framework -- dispatcher-only change.
#
#   RUN O -- --extra-tunables (both) + --reachable-scan + --phy-ip-
#            diag-at=none. RUN O is wedge-immune: it dumps 4-byte
#            read snapshots of rc_base +0..0x60, phy_common +0..0x40,
#            phy_shared +0..0x40, axi_base +0..0x40 at EVERY Phase F
#            diag checkpoint, and never reads phy_ip. Purpose: find
#            the reachable bit(s) that toggle when RUN J's rc_base
#            writes flip phy_ip's fault mode. Diff pre vs post 5.5
#            checkpoints identifies status bits (pll_locked,
#            phy_ready, etc.) we've been blind to.
#
#   RUN P -- --extra-tunables (both) + --phy-ip-write-probe + --phy-
#            ip-diag-at=post-5.5.extra-tunables. Same RUN J setup,
#            but the destructive probe at post-5.5.extra-tunables
#            is replaced with a naked posted write32 of 0 to phy_ip
#            + 0x38 (first pll tunable target). Read stalled RUN J
#            with SYNC; a posted write bypasses that. WROTE means
#            phy_ip decodes writes even if reads stall -- next
#            hypothesis: sequence the pll tunable writes via naked
#            write32 (not the RMW applicator) to see whether phy_ip
#            transitions to a state where reads succeed.
#
#   RUN Q -- --naked-write-test + --reachable-scan + --phy-ip-diag-
#            at=none. Wedge-immune bisection of RUN O's finding that
#            the m1n1 tunable applicator's writes to rc_base and
#            axi_base do NOT visibly change register state. Skips
#            --extra-tunables and does naked posted write32 to each
#            target address instead, with pre + post reads. Result
#            tags per target: STUCK (write took effect, m1n1
#            applicator is broken), NO-OP (fabric drops writes to
#            this block), or PARTIAL. --reachable-scan snapshots
#            state at every checkpoint so we can see how naked
#            writes propagate. No phy_ip touches.
#
#   RUN R -- same flag set as RUN Q, but the naked-write and reachable-
#            scan targets have been widened at the source (perstn.py's
#            _NAKED_WRITE_TARGETS + _REACHABLE_SCAN_WINDOWS). Hunts
#            for the cio3pllcore target block by first-touch probing
#            axi_sub5 (reg[5], 0x495046200) and axi_sub6 (reg[6],
#            0x495044000). Both fit cio3pllcore's max_off=0x100. Also
#            adds rc_base+0x54 (R/W bitmap readback at a known-live
#            offset) and rc_base+0x100 (cio3pllcore #6 candidate on
#            rc_base). Reachable-scan is first-touch-gated for
#            sub5/sub6 so early checkpoints stay safe. Analyzes the
#            log to decide RUN S: if sub5 or sub6 is writable, RUN S
#            applies cio3pllcore + pcieclkgen there via naked mask-
#            RMW; if not, RUN S falls back to the full 58-entry
#            axi2af naked mask-RMW apply (bypassing the broken
#            applicator that RUN Q showed is silently no-op'ing on
#            reg_idx=4).
#
#   RUN S -- --naked-write-test + --axi2af-naked-apply +
#            --pcieclkgen-naked-apply-to=axi_sub5_base +
#            --reachable-scan + --phy-ip-diag-at=none. Applies the
#            full 58-entry apcie-axi2af-tunables to axi_base via
#            naked mask-RMW (bypassing m1n1's applicator, which
#            RUN Q showed silently no-ops at reg_idx=4). Follows
#            with the 1-entry apcie-pcieclkgen-tunables applied to
#            axi_sub5 (RUN R identified sub5 as the CIO3 PLL Core
#            target block). cio3pllcore is skipped because RUN R
#            showed sub5 already carries cio3pllcore #0..#3 in the
#            reset state (re-applying would be a no-op). Success
#            criterion: step 6.g stops wedging at phy_ip_base+0x38.
#            The reachable-scan captures state at post-5.8.a
#            (post-axi2af) and post-5.8.b (post-pcieclkgen) so
#            cross-checkpoint diff shows what those writes toggled.
#
#   RUN 1 -- RUN S baseline + --phycmn-early. Smallest untried delta
#            from the RUN S state. RUN I proved --phycmn-early's
#            mask32(phy_common+0, MODE=ON) STICKS (phy_common+0
#            0x80300000 -> 0x80300001) but alone did not ungate
#            phy_ip; RUN S proved the naked-mask-RMW apply of
#            axi2af (14 STUCK / 42 SKIP-NOOP / 2 NO-OP at
#            axi_base+0x38/+0x40) plus pcieclkgen at axi_sub5+0
#            (STUCK) also alone did not ungate phy_ip. RUN 1 tests
#            the CO-application: CLK_MODE=ON after all the naked
#            applies land, immediately before the phy_ip access.
#            Same wedge-immune diag posture as RUN S (--reachable-
#            scan + --phy-ip-diag-at=none). Success criterion:
#            step 6.g stops wedging at phy_ip_base+0x38. Failure
#            case is still informative because the reachable-scan
#            captures state at every checkpoint including post-7.
#            phycmn-early (which is a new snapshot combination).
#
#   RUN 2 -- RUN 1 baseline + --cio3pllcore-naked-apply-to=axi_sub5_
#            base. Finishes what RUN S/R left half-done: RUN R
#            probed cio3pllcore entries #0..#3 on sub5 and marked
#            them SKIP-NOOP (pre-values already matched target),
#            then STOPPED. Entries #4 (+0x4c mask 0xff <- 0x94),
#            #5 (+0xe8 mask 0xe0000 <- 0x20000), and #6 (+0x100
#            mask 0xffffff <- 0xb40b4) were NEVER applied on any
#            base -- their offsets sit OUTSIDE the RUN R scan
#            window (0..0x40). Result: FALSIFIED. All 7 entries
#            SKIP-NOOP on sub5 (pre-values captured: #4 +0x4c
#            pre=0x1f800094 matches want 0x94; #5 +0xe8 pre=
#            0x01024201 matches want 0x20000; #6 +0x100 pre=
#            0x030b40b4 matches want 0xb40b4). iBoot already
#            programmed the CIO3 PLL analog block on sub5 before
#            m1n1 ran -- so axi_sub5 is NOT the missing target
#            for cio3pllcore. Wedge at phy_ip+0x38 unchanged.
#
#   RUN 3 -- RUN 2 baseline but redirects --cio3pllcore-naked-
#            apply-to from axi_sub5_base to phy_common_base per
#            the Asahi source hunt captured in docs/ref-asahi-
#            t8132-pcie.md (Section 8.1). Rationale: atc.c:882-
#            884 applies its common tunables to regs.core, and
#            phy_common is the direct PCIe analog. atc.c:112-114
#            places CIO3PLL_CLK_CTRL at regs.core + 0x2a00 and
#            CIO3PLL_DCO_NCTRL at +0x2a38 -- the same +0x38
#            alignment as our persistent phy_ip wedge address.
#            RESULT: FALSIFIED. cio3pllcore apply on phy_common
#            landed 0 STUCK / 1 PARTIAL / 5 NO-OP / 1 SKIP-NOOP;
#            widened reachable-scan phy_common +0x2a00..+0x2c00
#            (128 words) read all zeros -- no CIO3PLL block
#            discoverable at that offset in phy_common's window.
#            The atc.c layout does not port to PCIe's phy_common
#            on t8132. Target-block search for cio3pllcore is now
#            EXHAUSTED across all four candidate blocks (sub5,
#            sub6, rc_base, phy_common); local naked-apply
#            brute-forcing is retired. Wedge at phy_ip+0x38
#            unchanged. See Scripts/m1n1/logs/3/findings.md and
#            docs/project-m4-pcie-bringup.md for full analysis.
#
#   RUN 4 -- RUN 1 baseline (RUN S + phycmn-early) but replaces
#            --naked-write-test with --naked-write-test-readonly.
#            RUN 3's log analysis (nic-runtime.txt:1567-1573)
#            surfaced that every RUN S/1/2/3's first-touch probe
#            on axi_sub5+0 was DESTRUCTIVE: iBoot's pre-programmed
#            sub5+0 = 0x00081f55 PLL control word (bits 0/2/4/6/
#            8-12/19 populated) was clobbered to 0x00000a01 by
#            the full-word write, losing bits 2/4/6/8/10/12/19
#            (all outside cio3pllcore's target mask). If any of
#            those lost bits is a PLL enable or reference-clock
#            select, we have been gating our own PLL off before
#            Phase F even starts for the last 5 RUNs.
#            --naked-write-test-readonly runs the same target
#            list but does ONLY the pre-read stage (which flips
#            axi_sub{5,6}_reachable on success); the write +
#            post-read stages are skipped. This preserves the
#            iBoot state AND still enables the reachable-scan
#            widening + step 5.8.b (pcieclkgen naked apply on
#            axi_sub5) whose gates check the same reachability
#            flag. Everything else identical to RUN 1: same
#            --axi2af-naked-apply, --pcieclkgen-naked-apply-to=
#            axi_sub5_base, --phycmn-early, --reachable-scan,
#            --phy-ip-diag-at=none. Single-variable delta vs
#            RUN 1: only the destructive full-word write to
#            sub5+0 is gone.
#            Success criterion: step 6.g stops wedging at
#            phy_ip_base+0x38. Interpretation matrix:
#              - 6.g clean               -> sub5+0 clobber was
#                                            the blocker for
#                                            RUNs S/1/2/3;
#                                            proceed to Phase G
#              - 6.g wedges, sub5+0
#                shows iBoot 0x81f55
#                preserved except for
#                pcieclkgen mask-0x3e0  -> destructive-probe
#                bits (~0x81f75)          hypothesis definitively
#                                          ruled out; RUN 5 =
#                                          --phy-ip-write-probe
#                                          from cleaner state
#              - 6.g wedges, sub5+0
#                shows unexpected       -> pcieclkgen mask-RMW
#                state (e.g. pcieclkgen   was somehow gated on
#                bits didn't stick)       destructive probe
#                                          landing first;
#                                          refocus on write-
#                                          order interaction
#              - Pre-read STALLs on
#                sub5+0                 -> sub5 unreachable
#                                          without prior write
#                                          (fabric quirk); RUN 5
#                                          reverts --naked-write-
#                                          test and reconsiders
#
# All RUNs share the same base flags (no-pcie-init + preinit-probe
# + pmgr-enable + gate-poke + t8140-replay + phy-ip-diag + fuse-recon)
# so the log always contains the full Phase 0..F trail. What differs
# is the ONE hypothesis flag and the phy_ip-diag checkpoint.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Which wrapper actually drives m1n1. Defaults to the PCIe-focused perstn.sh;
# the SoC-first RUNs (25+) override this to soc_bringup.sh.
RUNNER=perstn.sh

RUN="${1:-}"
if [ $# -ge 1 ]; then
    shift
fi

BASE_FLAGS=(
    --no-pcie-init
    --preinit-probe
    --pmgr-enable
    --pmgr-per-port
    --gate-poke
    --phy-ip-probe
    --t8140-replay
    --phy-ip-diag
    --fuse-recon
)

case "${RUN^^}" in
    I)
        FLAGS=("${BASE_FLAGS[@]}"
               --phycmn-early
               --phy-ip-diag-at=post-7.phycmn-early)
        ;;
    J)
        FLAGS=("${BASE_FLAGS[@]}"
               --extra-tunables
               --phy-ip-diag-at=post-5.5.extra-tunables)
        ;;
    K)
        # RUN K probes at the LATER of the two write points so if
        # only the combination unblocks phy_ip, we see it here.
        # post-7.phycmn-early fires after step 5.5 AND after 6.f
        # AND after phycmn-early's CLK_MODE=ON write.
        FLAGS=("${BASE_FLAGS[@]}"
               --extra-tunables
               --phycmn-early
               --phy-ip-diag-at=post-7.phycmn-early)
        ;;
    L)
        FLAGS=("${BASE_FLAGS[@]}"
               --phy4-x10-early
               --phy-ip-diag-at=post-6.i.phy4-x10-early)
        ;;
    M)
        # RUN M: bisect RUN J's SYNC trigger. Apply only the 7-entry
        # cio3pllcore prop, skip the 1-entry pcieclkgen prop. Probe
        # phy_ip at the same checkpoint as RUN J so the fault mode
        # (silent stall vs SYNC) is directly comparable.
        FLAGS=("${BASE_FLAGS[@]}"
               --extra-tunables
               --extra-tunables-only=cio3pllcore
               --phy-ip-diag-at=post-5.5.extra-tunables)
        ;;
    N)
        # RUN N: complement of RUN M. Apply only the 1-entry pcieclkgen
        # prop (rc_base+0 mask 0x3e0 <- 0x220), skip cio3pllcore. Same
        # checkpoint as M/J so all three logs sit at the same probe
        # site and the fault mode differences (silent / SYNC) surface
        # cleanly on side-by-side comparison.
        FLAGS=("${BASE_FLAGS[@]}"
               --extra-tunables
               --extra-tunables-only=pcieclkgen
               --phy-ip-diag-at=post-5.5.extra-tunables)
        ;;
    O)
        # RUN O: wedge-immune diff snapshot. Apply full extra-tunables
        # (both cio3pllcore + pcieclkgen). Never probe phy_ip -- the
        # sentinel 'none' checkpoint skips it. Instead --reachable-scan
        # takes 4-byte snapshots of rc/phy_common/phy_shared/axi at
        # every Phase F diag checkpoint. Cross-checkpoint diff surfaces
        # the reachable bits toggled by the extra-tunables writes.
        FLAGS=("${BASE_FLAGS[@]}"
               --extra-tunables
               --reachable-scan
               --phy-ip-diag-at=none)
        ;;
    P)
        # RUN P: same setup as RUN J (both extra-tunables applied),
        # but replace the destructive phy_ip read probe at post-5.5.
        # extra-tunables with a naked posted write32 of 0 to phy_ip
        # + 0x38. Diagnoses whether phy_ip is on-fabric for writes
        # even in the RUN J state where reads SYNC-abort.
        FLAGS=("${BASE_FLAGS[@]}"
               --extra-tunables
               --phy-ip-write-probe
               --phy-ip-diag-at=post-5.5.extra-tunables)
        ;;
    Q)
        # RUN Q: naked-write bisection. Do NOT apply extra-tunables.
        # After step 5, do a naked write32 to each rc_base/axi_base
        # tunable target with the value the tunable would have set,
        # then read back. --reachable-scan gives us full state at
        # every checkpoint. --phy-ip-diag-at=none skips phy_ip -- this
        # RUN completes cleanly regardless of phy_ip state.
        FLAGS=("${BASE_FLAGS[@]}"
               --naked-write-test
               --reachable-scan
               --phy-ip-diag-at=none)
        ;;
    R)
        # RUN R: same flag set as RUN Q; the widening happens at the
        # source (perstn.py's _NAKED_WRITE_TARGETS gains sub5+0/sub6+0
        # first-touch probes plus rc_base+0x54/+0x100; the reachable
        # scan gains sub5/sub6 windows, gated by the first-touch flags
        # set inside probe_naked_write_test). No CLI-flag change vs
        # RUN Q -- the letter selects the widened target set at build
        # time.
        FLAGS=("${BASE_FLAGS[@]}"
               --naked-write-test
               --reachable-scan
               --phy-ip-diag-at=none)
        ;;
    S)
        # RUN S: naked mask-RMW apply of axi2af to axi_base +
        # pcieclkgen to axi_sub5. --naked-write-test still runs first
        # so its sub5 pre-read flips axi_sub5_reachable=True (which
        # step 5.8.b requires before applying pcieclkgen). Cio3pllcore
        # is deliberately skipped because RUN R showed sub5 already
        # holds cio3pllcore #0..#3 in reset state -- re-applying is a
        # no-op. Success criterion: 6.g no longer wedges.
        FLAGS=("${BASE_FLAGS[@]}"
               --naked-write-test
               --axi2af-naked-apply
               --pcieclkgen-naked-apply-to=axi_sub5_base
               --reachable-scan
               --phy-ip-diag-at=none)
        ;;
    1)
        # RUN 1: RUN S baseline + --phycmn-early. Tests whether
        # CLK_MODE=ON, applied AFTER the naked axi2af + pcieclkgen
        # applies and BEFORE the phy_ip access, is the missing
        # co-factor that ungates phy_ip. Single-variable delta vs
        # RUN S: only --phycmn-early is added. Neither RUN I
        # (--phycmn-early alone) nor RUN S (naked applies alone)
        # succeeded, so RUN 1 tests the combination that has never
        # been tried. Wedge-immune diag posture kept identical to
        # RUN S so cross-checkpoint diff vs RUN S surfaces exactly
        # what the CLK_MODE=ON write toggles when the naked apply
        # state has already landed.
        FLAGS=("${BASE_FLAGS[@]}"
               --naked-write-test
               --axi2af-naked-apply
               --pcieclkgen-naked-apply-to=axi_sub5_base
               --reachable-scan
               --phycmn-early
               --phy-ip-diag-at=none)
        ;;
    2)
        # RUN 2: RUN 1 baseline + --cio3pllcore-naked-apply-to=
        # axi_sub5_base. Applies the FULL 7-entry apcie-cio3pllcore-
        # tunables via naked mask-RMW to axi_sub5. RUN R stopped
        # after verifying entries #0..#3 SKIP-NOOP on sub5 (offsets
        # 0x00, 0x24, 0x28, 0x38 -- all within RUN R's 0..0x40 scan
        # window). Entries #4 (+0x4c mask 0xff <- 0x94), #5 (+0xe8
        # mask 0xe0000 <- 0x20000), and #6 (+0x100 mask 0xffffff
        # <- 0xb40b4) sit OUTSIDE that window and have NEVER been
        # applied to any base. Entry #6 is a 24-bit config write --
        # the substantial PLL analog config. Hypothesis: #4..#6
        # configure the CIO3 PLL analog block and are the missing
        # reference-clock config that leaves phy_ip un-clocked.
        # Reachable-scan window on axi_sub5/sub6 was widened from
        # 0..0x40 to 0..0x100 at the source so #4..#6 pre/post
        # state is captured at every Phase F checkpoint, including
        # the new post-5.8.c.cio3pllcore-naked-apply slot. Success
        # criterion: 6.g stops wedging at phy_ip_base+0x38.
        # RESULT: FALSIFIED. All 7 entries SKIP-NOOP on sub5;
        # iBoot pre-programmed sub5 to match cio3pllcore's target
        # values (pre-values captured in docs/project-m4-pcie-
        # bringup.md). Wedge at phy_ip+0x38 unchanged.
        FLAGS=("${BASE_FLAGS[@]}"
               --naked-write-test
               --axi2af-naked-apply
               --pcieclkgen-naked-apply-to=axi_sub5_base
               --cio3pllcore-naked-apply-to=axi_sub5_base
               --reachable-scan
               --phycmn-early
               --phy-ip-diag-at=none)
        ;;
    3)
        # RUN 3: RUN 2 baseline but redirects the cio3pllcore
        # naked apply from axi_sub5_base to phy_common_base per
        # the Asahi source hunt in docs/ref-asahi-t8132-pcie.md
        # Section 8.1. RUN 2 falsified sub5 as the cio3pllcore
        # target (all 7 entries SKIP-NOOP against iBoot-pre-
        # programmed sub5). RUN 3 tests the atc.c analogy:
        # atc.c:882-884 applies common tunables to regs.core;
        # phy_common is the direct PCIe analog. atc.c:112-114
        # places CIO3PLL_CLK_CTRL at regs.core+0x2a00 and
        # CIO3PLL_DCO_NCTRL at +0x2a38 -- the same +0x38 offset
        # as our persistent phy_ip wedge address. If phy_common
        # is the true target block, cio3pllcore's 7 entries
        # should land STUCK (unlike sub5's SKIP-NOOP). Same
        # wedge-immune diag posture as RUN 2 (--reachable-scan
        # + --phy-ip-diag-at=none). Reachable-scan on phy_common
        # is widened at the source with a second window at
        # +0x2a00..+0x2c00 so pre/post state of the suspected
        # CIO3PLL_CLK_CTRL (@+0x2a00) and CIO3PLL_DCO_NCTRL
        # (@+0x2a38) region is captured at every Phase F
        # checkpoint including post-5.8.c.cio3pllcore-naked-
        # apply. Success criterion: 6.g stops wedging at
        # phy_ip_base+0x38. Even on failure, the differential
        # phy_common data pin-points whether phy_common is the
        # target block (STUCK), whether iBoot already programmed
        # phy_common too (SKIP-NOOP -> RUN 4 tries rc_base or
        # phy_shared), or whether we hit a live register (SYNC).
        FLAGS=("${BASE_FLAGS[@]}"
               --naked-write-test
               --axi2af-naked-apply
               --pcieclkgen-naked-apply-to=axi_sub5_base
               --cio3pllcore-naked-apply-to=phy_common_base
               --reachable-scan
               --phycmn-early
               --phy-ip-diag-at=none)
        ;;
    4)
        # RUN 4: RUN 1 baseline but replaces --naked-write-test
        # with --naked-write-test-readonly. RUN 3 findings.md
        # surfaced that every RUN S/1/2/3 was destructively
        # clobbering iBoot's sub5+0 = 0x00081f55 PLL control word
        # to 0x00000a01 via the full-word first-touch probe. The
        # new READ-ONLY mode does only the pre-read (which flips
        # axi_sub{5,6}_reachable) and skips the write, preserving
        # iBoot state while still enabling downstream reachable-
        # scan widening AND step 5.8.b (pcieclkgen naked apply
        # at axi_sub5, gated on the same reachability flag).
        # Single-variable delta vs RUN 1: only the destructive
        # write to sub5+0 is gone. Success criterion: step 6.g
        # stops wedging. Failure with sub5+0 preserved except
        # for pcieclkgen mask-0x3e0 bits definitively rules out
        # the destructive-probe hypothesis and unblocks RUN 5 =
        # --phy-ip-write-probe from a cleaner state.
        FLAGS=("${BASE_FLAGS[@]}"
               --naked-write-test-readonly
               --axi2af-naked-apply
               --pcieclkgen-naked-apply-to=axi_sub5_base
               --reachable-scan
               --phycmn-early
               --phy-ip-diag-at=none)
        ;;
    5)
        # RUN 5: RUN 4 baseline + --phy-ip-write-probe with --phy-ip-
        # diag-at=post-7.phycmn-early. RUN 4 FALSIFIED the destructive-
        # probe hypothesis (sub5+0=0x00081f55 iBoot value preserved via
        # --naked-write-test-readonly; wedge at phy_ip+0x38 unchanged).
        # RUN 5 tests whether phy_ip decodes POSTED writes even when
        # reads AXI-stall, from the cleanest fabric state ever probed
        # for write-decode (naked axi2af + naked pcieclkgen landed at
        # sub5, CLK_MODE=ON, iBoot preserved except for pcieclkgen
        # mask-0x3e0 bits 5-9). RUN P tested this from a weaker
        # broken-applicator baseline where NO naked applies had landed;
        # RUN 5 tests from the strong state. Binary result hard-forks
        # the next 3-5 RUNs:
        #   WROTE -> RUN 6 = naked-write32 replay of 29 apcie-phy-ip-
        #            pll-tunables shared entries (write-only strategy,
        #            a full new attack surface).
        #   STALL -> phy_ip decode-locked in BOTH directions;
        #            RUN 6 = candidate B (pcieclkgen bit-5-only) or
        #            RUN 7 = candidate C (T8122 shared-init post
        #            writes, pcie.c:543-551).
        #   SYNC  -> writes decode but hit a live register with SError;
        #            pins fault mode on writes at phy_ip+0x38.
        # Wedge-immune posture: --phy-ip-write-probe replaces the
        # destructive read at post-7.phycmn-early with a posted
        # write32(phy_ip_base + 0x38, 0); a STALL costs no forward
        # progress vs the existing 6.g wedge (phy_ip is already
        # inaccessible there). Single-variable delta vs RUN 4:
        # --phy-ip-write-probe added, diag-at moved from none to
        # post-7.phycmn-early. Dispatcher-only, no perstn.py change.
        FLAGS=("${BASE_FLAGS[@]}"
               --naked-write-test-readonly
               --axi2af-naked-apply
               --pcieclkgen-naked-apply-to=axi_sub5_base
               --reachable-scan
               --phycmn-early
               --phy-ip-write-probe
               --phy-ip-diag-at=post-7.phycmn-early)
        ;;
    6)
        # RUN 6: RUN 4 baseline + --t8122-shared-post. RUN 5
        # FALSIFIED the write-decode hypothesis (STALL branch --
        # posted write32(phy_ip+0x38, 0) bus-hung m1n1 identically
        # to every prior read wedge; phy_ip is bidirectionally
        # decode-locked from the RUN 4 baseline). But RUN 5's
        # post-7.phycmn-early reachable-scan produced a specific
        # new data point: phy_shared+0x8 = 0x00000000 (bit 0
        # CLEAR) and phy_shared+0 = 0xf3c0301f (bits 8, 9 CLEAR).
        # pcie.c:543-551 has T8122/T602X/T6031 (but NOT T8140)
        # run:
        #     poll32(phy_shared+0x8, 1, 1, 250000)   ; wait
        #     set32(phy_shared+0, 0x200)             ; T8122 bit 9
        #     set32(phy_shared+0, 0x300)             ; T602X bit 8|9
        # RUN 6's new --t8122-shared-post block SKIPS the poll
        # (would just timeout 250 ms) and does the T8122-style
        # set32(phy_shared+0, 0x200) unconditionally after step 7.
        # Novel attack surface -- this pcie.c block has never
        # been replayed on t8132 (T8140 codepath skips it).
        # Hypothesis: bit 9 of phy_shared+0 is a phy_ip decode-
        # enable gate T8140 lacks but T8132 needs. If true,
        # step 6.g stops wedging for the first time in 23+ boots
        # and Phase F may complete for the first time ever. If
        # step 6.g still wedges identically, bit 9 is not the
        # ungate and RUN 7 = candidate B (pcieclkgen-set5-only
        # variant preserving iBoot bits 6, 8). Adds a new diag
        # checkpoint post-6.5.t8122-shared-post. Wedge-immune
        # posture retained: --phy-ip-diag-at=none, no phy_ip
        # probe. Single-variable delta vs RUN 4: only
        # --t8122-shared-post is added.
        FLAGS=("${BASE_FLAGS[@]}"
               --naked-write-test-readonly
               --axi2af-naked-apply
               --pcieclkgen-naked-apply-to=axi_sub5_base
               --reachable-scan
               --phycmn-early
               --t8122-shared-post
               --phy-ip-diag-at=none)
        ;;
    7)
        # RUN 7: RUN 6 baseline but replaces --pcieclkgen-naked-
        # apply-to=axi_sub5_base (mask 0x3e0 val 0x220) with
        # --pcieclkgen-set5-only-to=axi_sub5_base (set32(sub5+0,
        # 0x20) -- bit 5 only). RUN 6 FALSIFIED the bit-9-of-
        # phy_shared+0 hypothesis; bit 9 landed STUCK but phy_ip
        # remained bidirectionally decode-locked. Remaining top
        # hypothesis: pcieclkgen's mask-RMW has been clobbering
        # iBoot bits 6, 8 in axi_sub5+0 on every RUN S/1/2/3/4/
        # 5/6. iBoot pre-value 0x00081f55 has bits 6, 8 SET;
        # mask 0x3e0 covers bits 5-9; value 0x220 supplies 0
        # for bits 6, 8 -- so mask-RMW ALWAYS CLEARS them.
        # If bit 6 or bit 8 is a PLL enable, phy_ip has no
        # clock and every access AXI-stalls (matches the
        # persistent wedge signature exactly). RUN 7 preserves
        # bits 6, 8: post-value = 0x00081f75 (all iBoot bits +
        # bit 5). Single-variable delta vs RUN 6: only the
        # pcieclkgen mode switches from mask-RMW to bit-5-only;
        # --t8122-shared-post is retained even though RUN 6
        # proved it doesn't help (dropping it would be a second
        # variable change). Success criterion: step 6.g stops
        # wedging for the first time in 24+ boots. If it does,
        # pcieclkgen's mask-RMW has been the confounding
        # variable across 7 RUNs and we open a new attack
        # surface: audit every mask-RMW tunable for iBoot-bit
        # clobbers. If 6.g still wedges, bits 6/8 are not PLL
        # enables and RUN 8 = candidate C (--phy4-x10-early).
        # Wedge-immune posture retained: --phy-ip-diag-at=none.
        FLAGS=("${BASE_FLAGS[@]}"
               --naked-write-test-readonly
               --axi2af-naked-apply
               --pcieclkgen-set5-only-to=axi_sub5_base
               --reachable-scan
               --phycmn-early
               --t8122-shared-post
               --phy-ip-diag-at=none)
        ;;
    8)
        # RUN 8: RUN 7 baseline + --phy4-x10-early (candidate C).
        # RUN 7 FALSIFIED candidate B: the bit-5-only pcieclkgen
        # write landed STUCK (sub5+0: 0x00081f55 -> 0x00081f75,
        # iBoot bits 6, 8 PRESERVED for the first time since RUN S)
        # yet step 6.g wedged identically at phy_ip+0x38 entry #0
        # (UartTimeout). Bits 6, 8 of sub5+0 are not the phy_ip
        # clock/PLL enables (or not sufficient). Next-ranked
        # hypothesis: set32(phy_shared+4, 0x10) -- T602X/T8122's
        # pcie.c:529 write that the T8140 codepath skips. RUN L
        # tested it early-but-ALONE and wedged; it has never run
        # combined with the current strongest baseline (naked
        # axi2af + set5-only pcieclkgen + CLK0/1 ACKed + RESET
        # clear + T8140 marker + CLK_MODE=ON + phy_shared+0 bit 9).
        # perstn.py's existing 6.i.early block fires after 6.f and
        # before phycmn-early/6.5/6.g, which reproduces T8122's
        # native relative order pcie.c:529 -> 535 -> 543-551,
        # hoisted above the phy_ip tunables -- RUN 8 replays the
        # whole T8122 tail in T8122 order before the first phy_ip
        # touch. Expected transition: phy_shared+4 0x00000001 ->
        # 0x00000011 (RUN L saw the same bits stick). Single-
        # variable delta vs RUN 7: only --phy4-x10-early adds a
        # state-changing write; --t8122-shared-post and
        # --pcieclkgen-set5-only-to are retained even though
        # falsified as ungates (dropping either would be a second
        # variable change). Also adds --pmgr-pre6g-scan: a strictly
        # READ-ONLY PS-register sweep of apcie/pcie/phy/auspma/cio-
        # named PMGR devices immediately before 6.g, diffed against
        # the Phase 0 readout (feeds the power-domain hypothesis if
        # candidates C and D both fail). Wedge-immune posture
        # retained: --phy-ip-diag-at=none; the post-6.i reachable-
        # scan captures phy_shared (+0x8 included) right after the
        # new write at zero extra risk. If 6.g goes clean: bit 4 of
        # phy_shared+4 is the decode gate the T8140 codepath misses
        # on t8132 -- m1n1 patch candidate. If 6.g wedges
        # identically: candidate C falsified, RUN 9 = candidate D
        # (T602X-style set32(phy_shared+0, 0x300), bits 8+9).
        FLAGS=("${BASE_FLAGS[@]}"
               --naked-write-test-readonly
               --axi2af-naked-apply
               --pcieclkgen-set5-only-to=axi_sub5_base
               --reachable-scan
               --phycmn-early
               --phy4-x10-early
               --t8122-shared-post
               --phy-ip-diag-at=none
               --pmgr-pre6g-scan)
        ;;
    9)
        # RUN 9: RUN 8 baseline + --t8122-shared-post-val=0x300
        # (candidate D). RUN 8 FALSIFIED candidate C: the 6.i
        # set32(phy_shared+4, 0x10) landed STUCK (0x01 -> 0x11),
        # phy_shared+0x8 bit 0 never went active, and 6.g wedged
        # identically at phy_ip+0x38 entry #0 (UartTimeout). The
        # new pre-6.g PMGR sweep (39 matched devices) showed every
        # APCIE-family gate ON (actual=0xf) at the wedge point --
        # APCIE_GP / APCIE_SYS_GP / APCIE_ST / APCIE_SYS_ST /
        # APCIE_PHY_SW -- so no observable power domain blocks
        # phy_ip; the OFF devices are all unrelated ATC*/DPTX/CIO
        # Type-C tunnels. Next-ranked hypothesis: T602X's variant
        # of the shared-init post-write, set32(phy_shared+0, 0x300)
        # (pcie.c:549, bits 8+9), vs the T8122 0x200 (bit 9 only,
        # pcie.c:551) that RUNs 6/7/8 applied. Bit 8 has NEVER been
        # set on t8132. Expected transition: phy_shared+0
        # 0xf3c0301f -> 0xf3c0331f. Single-variable delta vs RUN 8:
        # only the 6.5 write value changes 0x200 -> 0x300; every
        # other flag retained (falsified-but-kept: --phy4-x10-early,
        # --pcieclkgen-set5-only-to, --t8122-shared-post; read-only:
        # --pmgr-pre6g-scan). Wedge-immune posture retained:
        # --phy-ip-diag-at=none. If 6.g goes clean: bit 8 (alone or
        # with 9) is the phy_ip decode gate -- m1n1 patch candidate.
        # If 6.g wedges identically: candidate D falsified and the
        # Python-replayable pcie.c write set is EXHAUSTED; RUN 10
        # pivots to the sequencing-gap axis (upload a stub and
        # p.call() it so the 29 pll-tunable writes execute
        # back-to-back on-CPU with barriers; fallback: port the
        # port-1 slice filter into m1n1's pcie_init_controller()
        # and let the C side run 6.g natively).
        FLAGS=("${BASE_FLAGS[@]}"
               --naked-write-test-readonly
               --axi2af-naked-apply
               --pcieclkgen-set5-only-to=axi_sub5_base
               --reachable-scan
               --phycmn-early
               --phy4-x10-early
               --t8122-shared-post
               --t8122-shared-post-val=0x300
               --phy-ip-diag-at=none
               --pmgr-pre6g-scan)
        ;;
    10)
        # RUN 10: RUN 9 baseline + --phy-ip-stub-apply=tail (tight-
        # timing-window hypothesis). RUN 9 FALSIFIED candidate D:
        # set32(phy_shared+0, 0x300) landed STUCK (0xf3c0331f, bits
        # 8+9 SET) yet 6.g wedged identically at phy_ip+0x38 entry
        # #0 (UartTimeout). With candidates A-D dead, the Python-
        # replayable pcie.c write set is EXHAUSTED. Key insight:
        # proxy read32/mask32 are executed by m1n1's CPU too, so
        # "proxy vs on-CPU" is identical at the instruction level --
        # the ONLY remaining sequencing variable is INTER-ACCESS
        # TIMING. pcie.c reaches the first phy_ip access
        # microseconds after the CLK0/CLK1 REQ+ACK handshake and
        # RESET deassert; our proxy replay takes SECONDS. If phy_ip
        # decode has a post-handshake time window, every RUN A..9
        # blew through it. RUN 10 uploads an AArch64 stub (ARMAsm,
        # aarch64-linux-gnu toolchain, upload_and_call.py pattern:
        # memalign + writemem + dc_cvau + ic_ivau + p.call) that
        # (a) re-runs the idempotent shared-init tail back-to-back
        # (CLK0REQ/ACK poll, CLK1REQ/ACK poll, RESET clear,
        # marker|0x11, phycmn MODE_ON, |0x300 -- all with dsb sy)
        # and (b) immediately applies the 29 pll tunable mask-RMWs,
        # then the 47 auspma entries (port-1-inactive slice
        # filtered, exactly like the Python path). Stub returns
        # 0xC0DE0000|count on success, 0xDEAD000x on tail poll
        # timeout / bad entry size; a wedge surfaces as UartTimeout
        # on the p.call step (fully flushed beforehand). Single-
        # variable delta vs RUN 9: only the 6.g/6.h application
        # method (+ the idempotent tail re-run microseconds before
        # -- same hypothesis axis). Interpretation: 0xC0DE001d +
        # live phy_ip verify reads = BREAKTHROUGH (timing window
        # confirmed; Phase F continues to 6.h/8/9/10);
        # UartTimeout = tight-timing axis falsified -- phy_ip does
        # not decode for the AP from ANY post-iBoot state pcie.c
        # writes can build; RUN 11+ = SMC/companion-processor
        # ownership hunt + aperture/security recon;
        # 0xDEAD0001/2 = a tail step is NOT idempotent (high
        # signal); SError/guard-delta = first-ever fault syndrome
        # from phy_ip.
        FLAGS=("${BASE_FLAGS[@]}"
               --naked-write-test-readonly
               --axi2af-naked-apply
               --pcieclkgen-set5-only-to=axi_sub5_base
               --reachable-scan
               --phycmn-early
               --phy4-x10-early
               --t8122-shared-post
               --t8122-shared-post-val=0x300
               --phy-ip-diag-at=none
               --pmgr-pre6g-scan
               --phy-ip-stub-apply=tail)
        ;;
    11)
        # RUN 11: full C-side pcie_init on the PATCHED m1n1 (commit
        # b404263: t8132 port-slice filter for the phy-ip tunables).
        # REQUIRES the patched m1n1.macho to be enrolled first
        # (kmutil configure-boot from recovery; /tmp/m4-serve has
        # m1n1.macho + m1n1.macho.prepatch rollback).
        #
        # Rationale: RUNs A..10 proved phy_ip is decode-locked from
        # every state the Python replay can build (all pcie.c
        # pre-phy_ip writes applied and STUCK, all APCIE PMGR gates
        # ON, on-CPU stub with tight timing -- all wedge at
        # phy_ip+0x38). But on 2026-07-11, p.pcie_init() ran to
        # completion (per-port bring-up included) and the C-side
        # applicator then walked all 29 pll entries through
        # phy_ip+0x38 cleanly. The unlock lives in the parts of
        # pcie_init the replay never executes (per-port bring-up
        # prime suspect). Its only known-fatal bug -- writing the
        # auspma port-1 slice on a machine with no pci-bridge1 --
        # is now filtered in C.
        #
        # NOT using BASE_FLAGS: no --no-pcie-init (p.pcie_init()
        # must run) and no --t8140-replay (Phase F would wedge at
        # 6.g before pcie_init). Keep the PERSTN/CLKREQ pokes and
        # Phase 0/D PMGR work; --tier3 arms the post-init dump
        # incl. the new Tier 3a phy_ip shared-window harvest;
        # LTSSM kick runs by default.
        #
        # Interpretation: pcie_init returns + filter printout shows
        # skipped port-1 entries + Tier 3a phy_ip reads live ->
        # C wedge fixed; check per-port LINKSTS (port 2 = NIC
        # training would be the jackpot; stuck BUSY -> we still
        # harvested a live phy_ip dump to diff against the replay
        # state for the unlock register). pcie_init wedges
        # elsewhere -> new C-side wedge address, high signal.
        # pcie_init returns but phy_ip still dead -> per-port-
        # unlock hypothesis falsified; fallback = cio3pllcore/
        # pcieclkgen naked-apply to rc_base (flags exist).
        FLAGS=(--preinit-probe
               --pmgr-enable
               --pmgr-per-port
               --gate-poke
               --tier3)
        ;;
    12)
        # RUN 12: RUN 11 retry with the Phase B landmine defused.
        # RUN 11 post-mortem: the patched m1n1 WAS enrolled (banner
        # v1.6.0-rc1-56-g6b277bc-dirty; raw-bin kmutil flow worked)
        # but with --pmgr-enable set and no --t8140-replay, legacy
        # Phase B ran alive for the first time in ~20 runs and its
        # shared-MMIO sweep included FOUR phy_ip reads (+0x0,
        # +0x8000, +0x10000, +0x18000) -- a pre-wedge-discipline
        # relic. m1n1 printed "Exception: SYNC" and died before
        # p.pcie_init() ever ran; the C-side port-slice filter is
        # STILL UNTESTED. Fix: Phase B's phy_ip probes removed in
        # perstn.py, and RUN 12 drops --pmgr-enable/--pmgr-per-port
        # entirely (pcie.c:425 does its own pmgr_adt_power_enable;
        # fewer live phases before the C init = fewer confounds,
        # closer to the 2026-07-11 environment). NO REFLASH NEEDED:
        # the patched m1n1 is already enrolled. Same interpretation
        # matrix as RUN 11: pcie_init returns + filter printout
        # ("applied N, skipped M (absent-port phy_ip slices)",
        # M > 0) + Tier 3a phy_ip harvest reads live -> check
        # per-port LINKSTS (port 2 = NIC training = jackpot; stuck
        # BUSY -> live phy_ip dump to diff for the unlock).
        # pcie_init wedges at a new C-side address -> high signal.
        # pcie_init returns but phy_ip still dead -> fallback =
        # cio3pllcore/pcieclkgen naked-apply to rc_base.
        FLAGS=(--preinit-probe
               --gate-poke
               --tier3)
        ;;
    13)
        # RUN 13: the ORDERING FIX. REQUIRES the RUN-13 m1n1 (fork
        # commit 7728fb0) to be enrolled first (kmutil from 1TR;
        # /tmp/m4-serve has m1n1.bin + m1n1.macho + rollbacks).
        #
        # RUN 12 post-mortem (see logs/12/findings.md): the wedge
        # inside pcie_init is 6b277bc's ORDERING BUG -- it applies
        # the phy-ip tunables at pcie.c:518, BEFORE per-port
        # bring-up, where phy_ip never decodes on t8132. pcie_up_2
        # (the first 6b277bc test, 2026-07-10) wedged identically;
        # pcie_up_1 (pre-6b277bc) returned 0 with ports at
        # LINKSTS_BUSY. The missing "ADT uses..." line was stuck in
        # the console buffer (only the first line escapes during a
        # proxy request), so the wedge LOOKED earlier than it was.
        # The port-slice filter (b404263) fixed a real but
        # second-order bug; the 29 shared pll entries still fired
        # pre-port-init and wedged first.
        #
        # RUN 13 recipe (every piece proven on this machine):
        #   1. m1n1 7728fb0 skips the phy-ip tunables in C on t8132
        #      -> pcie_init should return 0 like pcie_up_1.
        #   2. --post-init-phy-ip: pll via the C applicator
        #      (p.tunables_apply_local reg_idx=3 -- walked all 29
        #      entries cleanly on 2026-07-11 post-init) + auspma
        #      Python-side with the port-slice filter (the C
        #      applicator writing the port-1 slice was the
        #      2026-07-11 killer).
        #   3. Tier-3 dumps (Tier 3a phy_ip harvest verifies the
        #      tunables landed) + LTSSM kick + ECAM walk (NIC
        #      vendor/device ID = goal).
        # pcie_init runs with a 60 s UART timeout + 30 s
        # post-timeout liveness recovery (slow != dead).
        FLAGS=(--preinit-probe
               --gate-poke
               --tier3
               --post-init-phy-ip)
        ;;
    14)
        # RUN 14: RUN 13 done properly. RUN 13 burned a boot on a
        # STALE BINARY (banner still 56-g6b277bc-dirty -- the
        # tunables-skip build was staged but never kmutil-enrolled;
        # byte-identical re-run of RUN 12, ordering fix untested).
        # REQUIRES m1n1 8a569ad enrolled first (kmutil from 1TR;
        # /tmp/m4-serve has m1n1.bin + m1n1.macho; banner MUST read
        # v1.6.0-rc1-59-g8a569ad after boot). Hardening vs RUN 13:
        #   * --require-build=rc1-59-g -- perstn.py aborts BEFORE
        #     any device state change if the m1n1 USB product
        #     string doesn't match (no more silent stale boots).
        #   * m1n1 8a569ad adds PCIE_BC flushed breadcrumbs
        #     (printf + iodev_console_flush, the exception-handler
        #     delivery path) at every pcie_init stage -- if it
        #     still wedges, the last breadcrumb names the exact
        #     stage despite console-ring buffering.
        #   * --gate-poke DROPPED: Phase D's direct gate-151 poke
        #     was the one state-changing pre-init step the proven
        #     pcie_up_1 boot (p.pcie_init() -> 0) didn't have;
        #     pcie.c:425 does its own pmgr_adt_power_enable.
        # Experiment unchanged from RUN 13: tunables-skip pcie_init
        # (should return 0 like pcie_up_1) + --post-init-phy-ip
        # (pll via C applicator reg_idx=3, auspma Python-filtered)
        # + tier-3 dumps (Tier 3a phy_ip harvest) + LTSSM kick +
        # ECAM walk (NIC vendor/device ID = goal).
        FLAGS=(--preinit-probe
               --tier3
               --post-init-phy-ip
               --require-build=rc1-59-g)
        ;;
    15)
        # RUN 15: the 2026-07-11 recipe -- Phase F AFTER pcie_init.
        # NO REFLASH NEEDED (m1n1 8a569ad stays enrolled; the
        # require-build guard enforces it).
        #
        # RUN 14 results: p.pcie_init() -> 0 (first completed C init
        # since 2026-07-10) with every shared-init breadcrumb clean.
        # Both ports "failed to become idle" (LINKSTS BUSY within
        # 250 ms -> continue) -- a NON-event: pcie_up_1's post-init
        # LINKSTS (0x8300020c/0x83000204) had BUSY set too, so the
        # known-good boot had the same port outcome. But phy_ip was
        # STILL locked right after pcie_init: the straight-to-pll
        # post-init apply wedged at 0x497040038.
        #
        # The 2026-07-11 boot (the only phy_ip decode ever) did NOT
        # go straight from pcie_init to phy_ip: it re-ran the WHOLE
        # Phase F shared sequence first (pmgr enable, axi2af, phy
        # tunables, a SECOND CLK0REQ/ACK+CLK1REQ/ACK handshake,
        # RESET clear, T8140 marker) and only then applied the pll
        # tunables -- successfully. Hypothesis: the post-init
        # shared-sequence RE-PASS is the phy_ip ungate.
        #
        # RUN 15 = --t8140-replay-post-init: run Phase F (native
        # order, no experimental flags -- exactly d664bd9's Phase F)
        # after pcie_init returns. Phase F's 6.g applies the 29 pll
        # entries per-entry python-side; 6.h applies auspma with the
        # port-1 slice filtered (the 2026-07-11 killer). Then steps
        # 7-10, tier-3 dumps (Tier 3a phy_ip harvest), LTSSM kick,
        # ECAM walk (NIC vendor/device ID = goal). Also new: an
        # early tier-1 dump right after pcie_init captures the port
        # LINKSTS state RUN 14 never got.
        #
        # Matrix: Phase F completes -> tunables in, best state ever;
        # ports still BUSY after kick -> RUN 16 = LTSSM work from a
        # fully-tuned controller. 6.g wedges even post-init ->
        # 2026-07-11 reproduced except m1n1 binary internals; next
        # lever = diff d664bd9-era port-body pre-idle phy writes
        # (clear32 0x10 / set32 0x200/0x400) vs 8a569ad. Earlier
        # Phase F step wedges -> step label names it.
        FLAGS=(--preinit-probe
               --tier3
               --t8140-replay-post-init
               --require-build=rc1-59-g)
        ;;
    16)
        # RUN 16: RUN 15 with its two tooling interactions fixed.
        # NO REFLASH (m1n1 8a569ad stays enrolled; require-build
        # enforces).
        #
        # RUN 15 post-mortem: pcie_init -> 0 again, and the new
        # early tier-1 dump proved the post-init state is
        # REGISTER-IDENTICAL to pcie_up_1's golden dump (LINKSTS
        # 0x8300020c/0x83000204, PHY_CTRL 0x2300066f, phy+4 0x30,
        # PHYCMN 0x80300001 -- all 7 compared registers match).
        # But the Phase F post-init replay never ran: its entry
        # check (perstn.py ~3168) requires phaseD_gate151_active, a
        # python-side flag only Phase D (--gate-poke) sets, and
        # RUN 15 dropped that flag -- graceful silent skip. Then
        # the unconditional Tier 3a phy_ip harvest wedged the boot
        # at read32(0x497040000), costing the LTSSM kick and ECAM
        # sections. The 2026-07-11-recipe hypothesis is STILL
        # untested.
        #
        # Fixes (both python-side):
        #   * post-init PMGR gate verification: re-read every
        #     apcie power-gate PS register (safe reads) after
        #     pcie_init; if all real gates ACTIVE (they will be --
        #     "BC pmgr power enable done" proved the C walk), set
        #     phaseD_gate151_active=True from hardware truth so
        #     Phase F actually runs.
        #   * Tier 3a phy_ip harvest now gated on phaseF_shared_up
        #     (Phase F full success) -- a skipped/failed Phase F
        #     boot stays alive through the rest of tier 3, the
        #     LTSSM kick, and the ECAM walk.
        # Same experiment + matrix as RUN 15: Phase F post-init ->
        # 6.g 29 pll entries -> 6.h auspma filtered -> 7-10 ->
        # PHASE F SUCCESS -> Tier 3a harvest -> kick -> ECAM (NIC
        # vendor/device ID = goal). 6.g wedge -> diff d664bd9-era
        # port-body pre-idle phy writes vs 8a569ad / bisect.
        FLAGS=(--preinit-probe
               --tier3
               --t8140-replay-post-init
               --require-build=rc1-59-g)
        ;;
    17)
        # RUN 17: cold-boot Phase F with C-applicator 6.g -- the
        # CORRECTED 2026-07-11 reproduction. NO REFLASH (8a569ad;
        # with --no-pcie-init its tunables-skip patch is inert).
        #
        # RUN 16 falsified post-init+re-pass+per-entry (Phase F ran
        # 1->6.f post-init, largely idempotent, 6.g entry #0 wedge
        # unchanged). Corrected archaeology: c4c0b33's contemporary
        # message ("flushed step 6.g's post-marker at 29649 bytes,
        # then wedged before step 6.h") proves the Jul-11 boot
        # completed 6.g via p.tunables_apply_local(reg_idx=3) --
        # the C-side applicator, pre-filter era -- and the
        # "pcie_init ran first" belief was circular (pcie_up_2
        # proved 6b277bc's pcie_init wedges; Phase F was BUILT as
        # its replacement; --no-pcie-init existed at d664bd9).
        #
        # Combination matrix:
        #   cold + PhaseF + per-entry      = 30 wedges (A..10, 17-)
        #   post-init + PhaseF + per-entry = RUN 16 wedge
        #   post-init (no re-pass) + C-app = RUN 14 wedge
        #   cold + PhaseF + C-APPLICATOR   = UNTESTED <- RUN 17
        #
        # Flags mirror the Jul-11 environment (pre-init flow with
        # Phase D poke, Phase F native order, wedge-immune diag) +
        # --phyip-apply-local: 6.g via the C applicator (all 29 pll
        # entries are shared-slice, no port risk); 6.h stays
        # python-filtered (its port-1 slice killed Jul-11's 6.h).
        #
        # Matrix: 6.g returns 0 -> method confirmed, Phase F
        # continues (6.h filtered, 7-10, PHASE F SUCCESS) -> Tier 3a
        # gated harvest fires -> RUN 18 = ports/LTSSM from a tuned
        # controller. 6.g wedges -> cold+C-applicator falsified,
        # Jul-11 has no reproducible recipe left -> pivots: extra
        # tunables cio3pllcore/pcieclkgen->rc_base pre-6.g; m1n1
        # bisect Jul-10-era vs 8a569ad; or attack LTSSM directly
        # without phy-ip tunables.
        FLAGS=(--no-pcie-init
               --preinit-probe
               --gate-poke
               --t8140-replay
               --phy-ip-diag
               --reachable-scan
               --phy-ip-diag-at=none
               --phyip-apply-local
               --require-build=rc1-59-g)
        ;;
    18)
        # RUN 18: THE PIVOT. The phy-ip tunables axis is CLOSED after
        # 37 boots: RUN 17 (cold + C-applicator 6.g) wedged at entry
        # #0 with the exact +55-byte post.6.g flush signature that
        # the Jul-11 record shows -- and the d664bd9-era step()
        # flushed the post marker on the exception path too, so the
        # "only phy_ip decode ever" was a MISREAD WEDGE. phy_ip has
        # never decoded on this machine; 6b277bc's premise was never
        # validated.
        #
        # The evidenced blocker is the ORIGINAL one: pcie_init
        # (tunables-skip m1n1 8a569ad) returns 0, ports power up and
        # reach LTSSM BUSY -- training STARTS -- and never converge
        # within the C-side 250 ms idle poll, so the port body
        # continues out early. Historical kick data (pcie_up_1):
        # post-init writes to rc_base+0x3c / port+0x10 are SILENTLY
        # DROPPED (read back 0) and reset cycles don't move LINKSTS
        # -- the port config regs look write-locked while BUSY.
        #
        # Prime suspect: the CLKREQ# pin. The ADT declares
        # function_clkreq = GPIO(162, alt-func 2) -- the apcie
        # controller manages the CLKREQ#/refclk handshake itself --
        # but EVERY run since the pcie_up era forced the pin to a
        # manual GPIO output LOW ("emulating" the endpoint). A broken
        # refclk-request handshake is exactly the kind of thing that
        # leaves LTSSM spinning forever. RUN 18 restores the
        # ADT-declared pin function (--clkreq-mode=periph) before
        # the PERSTN cold reset and pcie_init.
        #
        # Also new: a 5 s post-init LINKSTS watch per active port
        # (the C-side poll only allows 250 ms; a slow endpoint would
        # look identical to a dead one). Then tier dumps (Tier 3a
        # phy_ip harvest auto-gated OFF via phaseF_shared_up=False),
        # LTSSM kick, and -- for the first time in the numeric era --
        # the ECAM walk on a live boot. NIC vendor/device ID = goal.
        #
        # Matrix: BUSY clears (watch or post-kick) -> LINK TRAINING
        # BREAKTHROUGH -> ECAM walk finds the NIC -> next phase = BAR
        # setup + driver. Still BUSY with periph CLKREQ -> the pin
        # override wasn't the (only) blocker; RUN 19 candidates:
        # PERST re-sequencing (re-toggle after APPCLK), longer
        # settle, per-port REFCLK setup (Linux apple_pcie_setup_
        # refclk analog), or --no-clkreq (leave iBoot pin state
        # entirely). ECAM readable despite BUSY -> port fabric alive,
        # training-only problem (high signal either way).
        FLAGS=(--preinit-probe
               --tier3
               --clkreq-mode=periph
               --require-build=rc1-59-g)
        ;;
    19)
        # RUN 19: link training, part 2. RUN 18's --clkreq-mode=periph did
        # NOT clear LINKSTS BUSY (0x8300020c / 0x83000204 held). Missing
        # piece: the per-port REFCLK REQ->ACK handshake on phy_base+0x000
        # (PHY_LANE_CFG) that Linux apple_pcie_setup_refclk does BEFORE
        # LTSSM trains -- perstn.py never performed it, relying entirely on
        # m1n1's C-side pcie_init + the CLKREQ# pin. phy_base+0x000 is
        # proven reachable (RUN 18 Tier-2 read 0x2300066f on both ports).
        #
        # setup_refclk runs in BOTH slots: 'pre' (before pcie_init, upstream
        # ordering) and 'post' (after pcie_init, kicking the link pcie_init
        # left stuck at BUSY -- the post-init 5 s LINKSTS watch then shows
        # whether BUSY clears). Keep RUN 18's ADT pin mux. Tier-3
        # phy_extra/ctrl_lo probes are now gated OFF by default (they wedged
        # m1n1 in RUN 18 at 0x497048000, before the LTSSM kick + ECAM walk),
        # so this run should finally reach the ECAM walk. REFCLKCGEN is left
        # off (--refclk-cgen would add it) to isolate the REQ->ACK handshake.
        #
        # Matrix: ACK asserts + BUSY clears -> link trains -> ECAM walk finds
        # the NIC -> next phase = BAR setup + driver. ACK times out -> refclk
        # gating is upstream of phy_base+0x000; RUN 20 = PERST re-toggle after
        # refclk / --no-clkreq+refclk / longer settle. ACK asserts but BUSY
        # persists -> refclk necessary-not-sufficient; combine with the LTSSM
        # kick / T602X replay. ECAM readable despite BUSY -> fabric alive,
        # training-only problem (high signal either way).
        FLAGS=(--preinit-probe
               --tier3
               --clkreq-mode=periph
               --setup-refclk=both
               --require-build=rc1-59-g)
        ;;
    20)
        # RUN 20: link training, part 3. RUN 19 proved the refclk REQ->ACK
        # handshake succeeds but LINKSTS stays BUSY. m1n1 src/pcie.c:857-889
        # shows WHY: the LTSSM kick (ltssm_base+0x10/0x1c/0x20/0x14) and the
        # "do it again" retry are BOTH gated to APCIE_T602X and skipped on the
        # T8140/APCIE path we run -- so LTSSM is never started and pcie_init
        # bails at the "failed to become idle" poll. ltssm_base read cleanly
        # (all-zero, no wedge) in RUN 19, so writing it is safe now.
        #
        # Replay m1n1's T602X controller==APCIE completion path via
        # --t602x-init --t602x-aggressive --t602x-do-again (t602x_port_init_
        # replay: rc_base+0x3c gate, port writes, T602X_RESET cycle, the
        # ltssm_base kick writes, rc_base+0x3c clear-to-arm), on top of the
        # RUN 19 refclk handshake. Keep the ADT pin mux; phy_extra probes
        # stay gated. A post-t602x LINKSTS watch + the ECAM walk report the
        # result.
        #
        # Matrix: BUSY clears -> LINK TRAINS -> ECAM walk finds the NIC ->
        # BAR setup + driver. Still BUSY -> the ltssm_base writes weren't the
        # trigger (or a further gate remains); RUN 21 = explicit PORT_LTSSMCTL
        # 0x080 START (Linux mechanism) / PERST re-toggle / endpoint-presence
        # diagnostics.
        FLAGS=(--preinit-probe
               --tier3
               --clkreq-mode=periph
               --setup-refclk=both
               --t602x-init
               --t602x-aggressive
               --t602x-do-again
               --require-build=rc1-59-g)
        ;;
    21)
        # RUN 21: link training, part 4. RUN 20 landed the LTSSM config
        # writes (ltssm+0x10/0x1c/0x20 latched) but LTSSM_START (ltssm+0x14)
        # refused to latch (written 0x1, read back 0) and LINKSTS stayed
        # BUSY; ECAM all vacant. Root cause: PERST# is deasserted BEFORE
        # refclk is up, violating the ADT's t-refclk-to-perst=100 /
        # perst-to-config=100 ordering, so the port rejects START.
        #
        # Re-sequence refclk-first (--perst-resequence): setup_refclk(post),
        # then re-assert PERST# (gpio165 low), cycle port+0x82c, deassert
        # PERST#, 100 ms settle, poll BUSY->0, write LTSSM_START and READ
        # ltssm+0x14 back -- the key instrument. Pin mux kept; phy_extra
        # probes stay gated. Drops --t602x-init (RUN 20 showed its
        # rc_base+0x3c/port+0x10 writes drop post-init and add noise); this
        # isolates the ordering variable.
        #
        # Matrix: ltssm+0x14 latches 0x1 / BUSY clears -> LINK TRAINS ->
        # ECAM finds the NIC. START still 0 after correct ordering -> START
        # gated deeper than reset timing; RUN 22 = patched m1n1 (in-window
        # T602X writes) or --no-clkreq A/B. port2 never distinguishes from a
        # dead port -> endpoint-presence axis (RUN 22 diagnostics).
        FLAGS=(--preinit-probe
               --tier3
               --clkreq-mode=periph
               --setup-refclk=both
               --perst-resequence
               --require-build=rc1-59-g)
        ;;
    22)
        # RUN 22: patched-m1n1 in-window T602X LTSSM enable. RUNs 18-21
        # exhausted the no-reflash axis: the T602X-gated writes (rc_base+0x3c
        # arm, port+0x10, ltssm+0x14 START) all refuse POST-init Python
        # writes -- RUN 20/21 seq-A even tried set32(rc_base+0x3c,0x1) and it
        # read back 0 (logs/21:720-721,738-739). They must run IN-WINDOW
        # during pcie_init. This build (src/pcie.c patch) runs arm + port+0x10
        # + the LTSSM kick (incl ltssm+0x14=START) + disarm on the t8132 path,
        # with the kick placed BEFORE the idle poll so its continue can't skip
        # it, and t8132 does not bail on the idle-poll timeout -- EXCLUDING the
        # phy_ip/PHY_CTRL writes that SError on j773g. PCIE_BC breadcrumbs name
        # any wedge line; watch the TTY console for the arm/ltssm+0x14 readback.
        #
        # REQUIRES the patched build enrolled. Guard substring updated to
        # --require-build=v1.6.0-42-g for the currently-enrolled m1n1
        # (v1.6.0-42-gdf2ae61, branch t8132-rebase; the survivable ~30-char USB
        # window substring). Earlier this run targeted rc1-60-g4b77755; the
        # RUN18-21 build was rc1-59-g. --endpoint-diag (read-only) reports SMC
        # power + MAC + LINKSTS bit3 delta.
        #
        # Matrix: arm reads back 0x1 in-window + ltssm+0x14=0x1 + BUSY clears
        # -> LINK TRAINS -> ECAM finds the NIC. arm latches but BUSY persists
        # -> kicked, no partner -> RUN 23 signal-integrity/longer wait. arm
        # still 0 even in-window -> deeper analog/clock gate -> RUN 23
        # cio3pllcore/pcieclkgen in-C. A PCIE_BC'd write wedges -> that line
        # named -> exclude + re-bisect.
        FLAGS=(--preinit-probe
               --tier3
               --clkreq-mode=periph
               --setup-refclk=both
               --endpoint-diag
               --require-build=v1.6.0-42-g)
        ;;
    23)
        # RUN 23: force APCIE_PHY_SW (gate 151) ACTIVE before pcie_init.
        # RUN 22 ran the T602X arm/LTSSM writes in-window but they read back
        # 0 -- and Phase 0 showed gate 151 (the t8132 PHY power switch, NOT
        # always-on) + parent APCIE_SYS_ST OFF. RUNs 18-22 all dropped
        # --gate-poke (Phase D), so the in-window arm may have run with the
        # PHY switch powered off. Re-add it: Phase D forces gate 151 + parents
        # ACTIVE (PMGR-only, wedge-safe) BEFORE pcie_init, so the patched
        # build's in-window arm/kick run with the PHY domain up. No reflash --
        # reuses the rc1-60-g build. --endpoint-diag now does a same-session
        # SMC readback + LTSSM-debug decode. Phase E stays OFF (no phy_ip:
        # --phy-ip-probe deliberately omitted).
        #
        # Matrix: with gate 151 ON, arm rc_base+0x3c reads back 0x1 /
        # ltssm+0x14=0x1 / BUSY clears -> the PHY-switch power was the missing
        # precondition -> link trains -> ECAM finds the NIC. Still 0 / BUSY ->
        # gate 151 ruled out; RUN 24 = T602X pmgr posture (pmgr_adt_power_
        # disable_index path,1 in-C, reflash) or the analog-PLL axis.
        FLAGS=(--preinit-probe
               --gate-poke
               --tier3
               --clkreq-mode=periph
               --setup-refclk=both
               --endpoint-diag
               --require-build=v1.6.0-42-g)
        ;;
    24)
        # RUN 24: SoC-first pivot. 6 falsified PCIe runs (18-23); two
        # "blockers" (rc_base+0x3c, ltssm+0x14) were write-strobe /
        # wrong-controller misreads. Every attempt ran on ONE core with the
        # ACIO companion IOP (iop,mxwrap-acio, role ACIO0 -- owner of the
        # CIO/PCIe PHY, the "CIO3 PLL" block that AXI-stalls) never booted.
        #
        # This run: --smp-start (start all M4 cores, never done), --soc-recon
        # (read-only recon of IOPs + asleep apcie/CIO power domains), and
        # --endpoint-diag (now sweeps the FULL ltssm_base LTSSM-debug window
        # for the LIVE state we've never actually read -- prior runs read our
        # own written config values). pcie_init still runs (returns 0, benign;
        # its in-window writes are strobes) so the LTSSM window is reachable in
        # its proven-safe post-init state. No IOP boot, no phy_ip, no reflash.
        #
        # Decides RUN 25: ACIO IOP powered-but-halted + RC parked-in-Detect ->
        # boot the ACIO rtkit IOP (PHY owner) before pcie_init. Cores refuse ->
        # fix SoC bring-up first. RC cycling -> endpoint-side.
        #
        # HISTORICAL / FROZEN: this arm still points at perstn.sh, but the
        # SoC-first flags below (--smp-start, --soc-recon) were moved OUT of
        # perstn.py into soc_bringup.py after this run completed (see logs/24/).
        # Re-running `perstn-run.sh 24` now would fail argparse on those two
        # flags. Use RUN 25 (soc_bringup.sh) for the SoC-first path going
        # forward; RUN 24 is retained only as the record of the pivot.
        FLAGS=(--smp-start
               --soc-recon
               --preinit-probe
               --tier3
               --clkreq-mode=periph
               --setup-refclk=both
               --endpoint-diag
               --require-build=v1.6.0-42-g)
        ;;
    25)
        # RUN 25: SoC-first bring-up moves to its own script, soc_bringup.py
        # (perstn.py was 6.7k lines and PCIe-focused; the SMP + companion-IOP
        # recon path now has its own home, sharing primitives via m4_common).
        #
        # RUN 24 found 9/9 secondary cores REFUSED and the ACIO IOPs
        # (ACIO0/1/3, iop,mxwrap-acio -- owners of the CIO3-PLL / phy_ip PHY
        # that AXI-stalls) gate-OFF. This run is READ-ONLY (except --smp-start),
        # no reflash: retry --smp-start, diagnose WHY the cores refuse
        # (--smp-diag: per-core PMGR CPU-gate state), and read the ACIO rtkit
        # CPU_STATUS/CPU_CONTROL (--acio-status) that RUN 24 skipped -- is the
        # PHY-owning IOP running, stopped, or in reset? No IOP boot.
        #
        # Decides RUN 26: ACIO stopped-but-powered -> boot the ACIO rtkit IOP
        # before pcie_init. Cores power-gated -> fix cluster power first. Cores
        # ON but refused -> spin-table / reset-vector path.
        RUNNER=soc_bringup.sh
        FLAGS=(--smp-start
               --soc-recon
               --smp-diag
               --acio-status
               --require-build=v1.6.0-42-g)
        ;;
    26)
        # RUN 26: RUN 25 established the refusing cores are POWERED (ECPU/PCPU
        # PMGR actual=0xf) but never take the spin-table -- a reset-vector /
        # handoff issue, NOT power (refutes RUN 24's "fix cluster power"). RUN
        # 25 also WEDGED m1n1 on --acio-status by reading acio-cpu0's un-clocked
        # ASC block (guarded() can't catch an AXI stall); acio_status now
        # gate-checks PMGR state before any MMIO, so it's safe and just reports
        # "ACIO un-clocked".
        #
        # This run (read-only, no reflash): --smp-probe harvests per-CPU RVBAR
        # (cpu-impl-reg[0]) + LOCK bit + the CPU-start block words, mirroring
        # m1n1 smp.c's address math, to see whether RVBAR delivery/enable
        # latching is correct. --acio-status is dropped from the default (now
        # only re-confirms un-clocked; still available manually).
        #
        # Decides the SMP fix (a separate m1n1-source + reflash task): RVBARs
        # sane+unlocked yet flag never set -> M4 per-part init/chicken gap
        # (chickens.c features_m4). Wrong die-1 RVBAR / unlatched enables ->
        # address-math bug in smp.c.
        RUNNER=soc_bringup.sh
        FLAGS=(--smp-start
               --smp-diag
               --smp-probe
               --soc-recon
               --require-build=v1.6.0-42-g)
        ;;
    27)
        # RUN 27: no-reflash test of the RVBAR-lock hypothesis (H1). RUN 26
        # proved every core's RVBAR is correct (== _vectors_start) but LOCKED,
        # and the UART never printed "RVBAR entry on secondary CPU" -- the cores
        # never leave reset. m1n1 only re-writes RVBAR (which clears RVBAR_LOCK,
        # smp.c:161) inside `if (cpu_features->cyc_ovrd)`, and features_m4 omits
        # cyc_ovrd, so on M4 that unlock is skipped.
        #
        # --smp-release-probe manually replicates smp_start_cpu for ONE core
        # (default reg=0x1): clears its RVBAR_LOCK via write64, then strobes the
        # CPU-start register -- the same writes m1n1 does, no reflash. Watch the
        # TTY> console for "RVBAR entry on secondary CPU": if it appears, the
        # core left reset -> H1 CONFIRMED -> RUN 28 = minimal smp.c fix + reflash.
        # (--smp-start first records the normal "Failed!" on the same boot;
        # --smp-probe re-captures the pre-release RVBAR/LOCK state.)
        RUNNER=soc_bringup.sh
        FLAGS=(--smp-start
               --smp-probe
               --smp-release-probe
               --require-build=v1.6.0-42-g)
        ;;
    28)
        # RUN 28: find the M4 core-release register (READ-ONLY). RUN 27 killed
        # the RVBAR-lock hypothesis (the lock is sticky-until-reset AND non-
        # blocking -- the boot core runs locked). The lead: every /cpus node
        # has function-enable_core = 138:Core(<core-bit>), a PMGR (phandle 138,
        # pmgr1,t8132) release recipe iBoot uses and m1n1's smp_start_cpu never
        # invokes (it only writes the legacy pmgr+0x34000 strobe). The 'Core'
        # function has no register impl in m1n1/Linux, so the register is found
        # by a live running-vs-waiting differential:
        #   --enable-core-parse : decode the recipe (ADT-only, zero MMIO).
        #   --core-diff-scan    : diff cpu6(running) vs cpu7(waiting), same
        #                         cluster, across the shared acc/cpm windows +
        #                         cpu-impl+0x100 status. A shared-window bit set
        #                         for cpu6 / clear for cpu7 = the enable register.
        # No writes this run. Candidate -> RUN 29 gated write test.
        RUNNER=soc_bringup.sh
        FLAGS=(--smp-start
               --smp-probe
               --enable-core-parse
               --core-diff-scan
               --require-build=v1.6.0-42-g)
        ;;
    29)
        # RUN 29: test the wrong-CPU-start-offset hypothesis (H5), READ-ONLY.
        # RUN 28 confirmed the function-enable_core = pmgr:Core(1<<cpu_id) recipe
        # (a FLAT per-core bitmask, unlike m1n1's per-cluster 1<<core strobe),
        # and its --core-diff-scan SError'd + wedged m1n1 reading the per-cluster
        # acc-impl window (0x211F00000) -- proof M4 relocated per-cluster MMIO.
        # A full m1n1 branch-diff proved the SMP fix is NOT upstream (our smp.c
        # already matches/exceeds it; features_m4 is // XXX everywhere; no
        # sysreg-unlock exists). Leading hypothesis: m1n1's CPU-start offset
        # 0x34000 (CPU_START_OFF_T8112, an unverified M2/M3 guess for T8132) is
        # wrong for M4, so the strobe never releases the core (zero UART output).
        #
        # --cpustart-decode dumps+decodes the CPU-start block at pmgr+0x34000
        # (proven-readable pmgr space) vs the running/waiting core set, under
        # both per-cluster and flat cpu_id encodings. NO --core-diff-scan (it
        # SErrors). Power-cycle the M4 first (RUN 28 left it wedged).
        # Outcome -> RUN 30: corrected CPU_START_OFF_T8132 / flat strobe + reflash.
        RUNNER=soc_bringup.sh
        FLAGS=(--smp-start
               --smp-probe
               --cpustart-decode
               --require-build=v1.6.0-42-g)
        ;;
    30)
        # RUN 30: find the REAL M4 CPU-start register (READ-ONLY). RUN 29
        # confirmed pmgr+0x34000 is inert on M4 (after --smp-start the running
        # core cpu6's bit is absent under every encoding at every offset).
        # KEY: upstream m1n1 gave the sibling M4 die T6040 a DIFFERENT offset
        # (CPU_START_OFF_T6031 = 0x88000), while our T8132 kept the unverified
        # 0x34000. --cpustart-decode now probes a CANDIDATE offset and flags any
        # word whose bits EXACTLY match the running core set -> the real
        # register.
        #
        # This arm probes 0x88000 (T6040, strongest candidate) first. A wrong
        # offset may SError and wedge m1n1, so run ONE candidate per boot and
        # POWER-CYCLE between. Other candidates (re-run with the override):
        #   ./perstn-run.sh 30 --cpustart-offset=0x30000   (then 0x38000,
        #   0x54000, 0x28000). A candidate whose word matches cpu6 -> RUN 31 =
        #   reflash smp.c with CPU_START_OFF_T8132=<that offset> + case T8132.
        RUNNER=soc_bringup.sh
        FLAGS=(--smp-start
               --smp-probe
               --cpustart-decode
               --cpustart-offset=0x88000
               --require-build=v1.6.0-42-g)
        ;;
    31)
        # RUN 31: LAST read-only offset search for the M4 CPU-start register.
        # RUN 29 proved pmgr+0x34000 is inert; RUN 30 eliminated 0x88000
        # (all-zero). Research: the register is BOOTLOADER-PRIVATE -- Linux
        # t8132 uses spin-table (cores pre-released by iBoot), no dtsi/ADT names
        # it, it's not in the AIC. --cpustart-scan does a curated bounded scan
        # of the pmgr window (known SoC CPU_START_OFF banks + a 0x34000
        # neighborhood) for the word that reflects the running core (== flat
        # 0x40 / per-cluster 0x10). It aborts on the first SError (proxy
        # desync); POWER-CYCLE the M4 first and between runs.
        #
        # Outcome fork: candidate found -> RUN 32 = reflash CPU_START_OFF_T8132.
        # Nothing found / SError abort -> the release is NOT an AP-visible pmgr
        # strobe -> STOP guessing; reassess whether SMP is needed for PCIe, or
        # escalate to Asahi/m1n1 devs with the RUN 24-31 evidence.
        RUNNER=soc_bringup.sh
        FLAGS=(--smp-start
               --smp-probe
               --cpustart-scan
               --require-build=v1.6.0-42-g)
        ;;
    *)
        echo "usage: $0 {I|J|K|L|M|N|O|P|Q|R|S|1|2|3|4|5|6|7|8|9|10|11|12|13|14|15|16|17|18|19|20|21|22|23|24|25|26|27|28|29|30|31} [extra perstn.py/soc_bringup.py args...]" >&2
        echo "" >&2
        echo "See the file header for what each RUN tests." >&2
        exit 1
        ;;
esac

exec "$SCRIPT_DIR/$RUNNER" "${FLAGS[@]}" "$@"
