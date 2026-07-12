#!/bin/bash
#
# perstn-run.sh -- dispatch a specific PCIe bring-up RUN by letter.
#
# Usage:
#   ./Scripts/m1n1/perstn-run.sh {I|J|K|L|M|N|O|P|Q|R|S} [extra perstn.py args...]
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
    *)
        echo "usage: $0 {I|J|K|L|M|N|O|P|Q|R|S} [extra perstn.py args...]" >&2
        echo "" >&2
        echo "See the file header for what each RUN tests." >&2
        exit 1
        ;;
esac

exec "$SCRIPT_DIR/perstn.sh" "${FLAGS[@]}" "$@"
