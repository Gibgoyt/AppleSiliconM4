#!/bin/bash
#
# perstn-run.sh -- dispatch a specific PCIe bring-up RUN by letter or number.
#
# Usage:
#   ./Scripts/m1n1/perstn-run.sh {I|J|K|L|M|N|O|P|Q|R|S|1|2|3} [extra perstn.py args...]
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
# per the atc.c CIO3PLL analogy in docs/ref-asahi-t8132-pcie.md.
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
#            If PCIe's phy_common is the analog-core-of-record
#            for the PCIe CIO3PLL block, cio3pllcore's 7 entries
#            should land STUCK on phy_common (they SKIP-NOOP'd
#            on sub5 in RUN 2 because iBoot pre-programmed that
#            block). Same wedge-immune diag posture as RUN 2
#            (--reachable-scan + --phy-ip-diag-at=none). The
#            reachable-scan window on phy_common has been
#            widened at the source to include a second block at
#            +0x2a00..+0x2c00 (128 words, ~100 ms UART) so the
#            suspected CIO3PLL_CLK_CTRL (@+0x2a00) and
#            CIO3PLL_DCO_NCTRL (@+0x2a38) region is captured
#            pre/post the naked apply. Success criterion: step
#            6.g stops wedging at phy_ip_base+0x38. Even on
#            failure, differential data pin-points the target
#            block. Interpretation matrix:
#              - 6.g clean            -> phy_common is the target
#                                        block AND cio3pllcore
#                                        was the missing config;
#                                        proceed to Phase G
#              - 6.g wedges, entries
#                STUCK on phy_common  -> phy_common IS the target
#                                        block; some OTHER config
#                                        (CIO3PLL_CLK enable pair
#                                        per atc.c:1778-1779?) is
#                                        still missing. RUN 4 =
#                                        atc.c-style CIO3PLL clock
#                                        enable at phy_common+0x2a00
#              - 6.g wedges, entries
#                SKIP-NOOP on phy_    -> phy_common ALSO already
#                common                  pre-programmed by iBoot;
#                                        RUN 4 = try rc_base then
#                                        phy_shared as target
#              - Any entry raises
#                SYNC / delta         -> hit a live register in
#                                        phy_common; directly
#                                        informative, narrows the
#                                        target block on offset
#
# All RUNs share the same base flags (no-pcie-init + preinit-probe
# + pmgr-enable + gate-poke + t8140-replay + phy-ip-diag + fuse-recon)
# so the log always contains the full Phase 0..F trail. What differs
# is the ONE hypothesis flag and the phy_ip-diag checkpoint.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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
    *)
        echo "usage: $0 {I|J|K|L|M|N|O|P|Q|R|S|1|2|3} [extra perstn.py args...]" >&2
        echo "" >&2
        echo "See the file header for what each RUN tests." >&2
        exit 1
        ;;
esac

exec "$SCRIPT_DIR/perstn.sh" "${FLAGS[@]}" "$@"
