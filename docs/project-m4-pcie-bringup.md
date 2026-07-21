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

**State as of 2026-07-12 (post RUN H):**
- Instrumentation win: probe flush-after-header + rc_base+0x024 pre/post read-back both landed cleanly. Log now ends at the exact phy_ip probe entry, and we have hard data on whether the RUN write's target register retains BIT(0).
- **rc_base+0x024 does NOT retain BIT(0).** `val_pre = 0x0`, `set32(..., BIT(0))` returns 1 (guard delta=0), `val_post = 0x0`. Either R/O in the current gate state or self-clearing. Either way, RUN G's wedge was NOT the write staying set -- it was the phy_ip probe that came after.
- **phy_ip STILL AXI-stalls at post-6.f.T8140-marker.** All 6 phy-common-diag checkpoints REACHABLE with val=0x80300000 unchanged. Every T8140 shared-init step through 6.f completed with delta=0. The T8140 codepath (steps 1..6.f) plus the T81XX PHYIF_CTRL_RUN write (step 6.f.5) does NOT ungate phy_ip on t8132.
- **Full pcie.c study (2026-07-12):** T8140 branch shared init is identical to T8122-compat except for two writes T8140 skips: (i) `set32(phy_base+4, 0x10)` at pcie.c:529 (T8122 step 6.i, runs AFTER phy_ip tunables on T8122); (ii) T8122's `poll(phy_base+0x8, 1, 1)` + `set32(phy_base+0, 0x200)` at pcie.c:543-551 (also after 6.i). Step 7 (phy_common CLK_MODE=ON at pcie.c:535) runs AFTER phy_ip tunables on both T8140 and T8122 -- so on those chips phy_ip must be accessible BEFORE CLK_MODE=ON. On t8132 the opposite may be true: phy_ip is gated on CLK_MODE=ON.
- Next iteration bisection ladder (RUNs I-L, delivered 2026-07-12): four single-hypothesis experiments dispatched via `Scripts/m1n1/perstn-run.sh {I|J|K|L}`. Each RUN holds every other Phase F variable constant vs RUN H and probes phy_ip at the checkpoint immediately after the one write it's testing.
  - **RUN I -- `--phycmn-early`:** apply step 7 (mask32(phy_common+0, MODE=1)) AFTER step 6.f, BEFORE any phy_ip access. Probes at `post-7.phycmn-early`. Hypothesis: CLK_MODE=ON is the phy_ip ungate on t8132.
  - **RUN J -- `--extra-tunables`:** apply apcie-cio3pllcore-tunables + apcie-pcieclkgen-tunables (t8132-specific ADT props no pcie.c branch touches) after step 5. Probes at `post-5.5.extra-tunables`. Hypothesis: these supply the missing PCIe clock/PLL config.
  - **RUN K -- I + J bundled:** only meaningful if I and J both fail alone -- tests for an interaction unlock. Probes at `post-7.phycmn-early` (later checkpoint).
  - **RUN L -- `--phy4-x10-early`:** apply set32(phy_shared+4, 0x10) (T8122 step 6.i, pcie.c:529, T8140-skipped) AFTER step 6.f, BEFORE phy_ip access. Probes at `post-6.i.phy4-x10-early`. Hypothesis: this write, which T8140 skips entirely, gates phy_ip on t8132.
- RUN I framework commit also lands a pre/post read-back around step 6.f (parity with RUN H's rc_base+0x024 pattern) and an `applied apcie tunables report` in the ADT recon so we can eyeball axi2af/common/phy tunables for hidden clock enables landing in the phy_shared or phy_common windows.

**State as of 2026-07-12 (post RUNs I-L):**
- All four RUN I-L hypotheses failed to unblock phy_ip. Every RUN's `[phy-common-diag @ ...]` checkpoint reached REACHABLE cleanly with val=0x80300000 (unchanged since RUN A); every RUN's `[phy-ip-diag @ post-<checkpoint>]` header landed on disk followed by an AXI stall (RUNs I/K/L silent, RUN J with `Exception: SYNC`).
- **RUN I -- `--phycmn-early` (phy_common CLK_MODE=ON before phy_ip):** phy_common+0 transitioned 0x80300000 -> 0x80300001 after step 7's mask32 (CLK_MODE=ON stuck), but phy_ip still silent-stalled. CLK_MODE=ON is not the phy_ip ungate on t8132.
- **RUN J -- `--extra-tunables` (cio3pllcore + pcieclkgen applied to rc_base):** phy_ip's failure mode CHANGED from silent AXI stall to `Exception: SYNC`. Both extra-tunables applied cleanly (delta=0 through step 5.5.b). This is the biggest signal since RUN F: one of the 8 rc_base writes (cio3pllcore's 7 + pcieclkgen's 1) flips phy_ip from unclocked/unmapped (silent hang) to reachable-but-erroring (SYNC decode). RUN J did NOT achieve REACHABLE, but it's the first evidence phy_ip is on the fabric at all in some state.
- **RUN K -- I + J bundled:** silent AXI stall at post-7.phycmn-early (RUN I's checkpoint). The CLK_MODE=ON write between the extra-tunables and the phy_ip probe reverted J's fault-mode change back to silent. Meaningful: whatever RUN J flipped is fragile to a subsequent phy_common write.
- **RUN L -- `--phy4-x10-early` (T8122 step 6.i's phy_shared+4 <- 0x10 moved early):** phy_shared+4 transitioned 0x00000001 -> 0x00000011 (bit 4 stuck), phy_ip still silent-stalled. T8122's 6.i is not the phy_ip ungate on t8132.
- **Consolidated finding across A-L:** The T8140 replay through step 6.f + any of {phy_common CLK_MODE, phy_shared+4 BIT(4), T81XX PHYIF_CTRL_RUN} does NOT ungate phy_ip on t8132. Only the extra-tunables (cio3pllcore/pcieclkgen) change any downstream state on phy_ip. Next bisection ladder (M-P) narrows into RUN J's SYNC anomaly rather than shopping for more T81XX/T8122 writes to try.
- Next iteration ladder (RUNs M-P, delivered 2026-07-12): four experiments dispatched via `Scripts/m1n1/perstn-run.sh {M|N|O|P}`. Each single-variable vs RUN J baseline.
  - **RUN M -- `--extra-tunables-only=cio3pllcore`:** apply ONLY the 7-entry cio3pllcore prop, skip pcieclkgen. Probe at `post-5.5.extra-tunables`. Hypothesis: cio3pllcore alone flips the fault mode.
  - **RUN N -- `--extra-tunables-only=pcieclkgen`:** apply ONLY the 1-entry pcieclkgen prop, skip cio3pllcore. Complement of M. Hypothesis: pcieclkgen alone flips the fault mode. Since it's 1 write to rc_base+0 mask 0x3e0 <- 0x220, if RUN N fires the SYNC, we've pin-pointed bits 5..9 of rc_base+0 as the phy_ip state gate.
  - **RUN O -- `--reachable-scan --phy-ip-diag-at=none`:** apply full extra-tunables (RUN J baseline). Skip phy_ip probe entirely (wedge-immune). At every Phase F diag checkpoint, snapshot rc_base +0..0x60, phy_common +0..0x40, phy_shared +0..0x40, axi_base +0..0x40 with per-address guard+alive checks. Diff pre vs post 5.5 checkpoints locates the reachable status bits toggled by the extra-tunables writes. This RUN alone can't fail; its value is in the differential data.
  - **RUN P -- `--phy-ip-write-probe`:** apply full extra-tunables (RUN J baseline). At post-5.5.extra-tunables replace the destructive read with a naked posted `write32(phy_ip_base + 0x38, 0)`. Diagnoses whether phy_ip is on-fabric for writes. WROTE opens a new lever: naked-write32 sequence of the pll tunables (not the RMW applicator). STALL/SYNC ties phy_ip access dead in both directions and refocuses the search upstream.
- Priority order for boots: **O first** (no risk, widest signal), **M or N second** (bisect the trigger), **P last** (needs the M/N result to interpret). All four can be re-run cheaply if a boot pattern hangs the fabric prematurely.

**State as of 2026-07-12 (post RUN O):**
- RUN O completed cleanly through post-6.f.T8140-marker and wedged on the FIRST phy_ip touch at step 6.g entry #0 (`p.mask32(0x497040038, 0x10000000, 0)`) with `UartTimeout: Expected 1 bytes, got 0 bytes`. Same failure mode as every prior RUN's phy_ip access: mask32's internal read stalls the fabric, m1n1 UART-freezes, Python-side times out.
- **Biggest surprise: the extra-tunables writes DON'T land.** post-5.phy-tunables vs post-5.5.extra-tunables reachable-scan diff shows rc_base+0/0x24/0x28/0x38 unchanged (all still `0x00040000` / `0` respectively). cio3pllcore #0 was supposed to set `rc_base+0` to `0x00040a01`. cio3pllcore #1/#2 to set `rc_base+0x24/0x28` to `0x800`/`0xb00`. None visible. The `Exception: SYNC` observed in RUN J was NOT the result of any of the 8 rc_base writes taking effect -- it was a phy_ip probe read hitting a different fault mode than the silent stall (likely address-specific: phy_ip+0 SLVERR-decodes while phy_ip+0x38 unmapped).
- **Step 2 (axi2af tunables) also doesn't land visibly.** axi_base+0 stayed `0x0000001c` across every scan (tunable #0 wanted to set bit 31 to `0x8000001c`). ~58 axi2af entries; none of the ones we can see hit our scan window changed value.
- **Which writes DO stick:** step 3 (`p.write32(rc_base+0x4, 0)`) -- rc_base+0x4 went from `0x4` to `0x0` cleanly. Step 5 (apcie-phy-tunables, `tunables_apply_local` with reg_idx=2 = phy_packed_base) -- phy_shared+0 bit 26 cleared (`0xf7c03090` -> `0xf3c03090`). Steps 6.a-6.f (set32/clear32 on phy_shared) -- all bits landed as expected.
- **Pattern:** writes via naked `p.write32()` stick; writes via `set32/clear32` stick; writes via `tunables_apply_local(reg_idx=2)` (phy_packed) stick; writes via `tunables_apply_local(reg_idx={1,4})` (rc_base, axi_base) silently no-op. Two hypotheses: (a) m1n1's applicator is broken for these specific reg indices on t8132, or (b) the RC/AXI blocks silently drop writes until some upstream gate is opened.
- **Non-signal notes:** rc_base+0x4c is a monotonically-increasing counter (`0x8a4a` at F.entry -> `0x9883` at post-6.f, tick of ~0x300 per snapshot). Not a control register. phy_common stays flat at `0x80300000` from F.entry through post-6.f -- no phy_ip status bit anywhere in phy_common+0..0x40.
- Next iteration (RUN Q, delivered 2026-07-12): naked-write bisection dispatched via `Scripts/m1n1/perstn-run.sh Q`. Skips extra-tunables entirely. After step 5, does `p.write32()` naked to the same rc_base and axi_base target addresses with the exact values the tunables were supposed to land. Reads pre AND post. Result STUCK means the applicator is broken (next RUN R re-applies via naked writes); NO-OP means the fabric drops writes (widens the search to phy_packed+0x100..0x300 and config_base reg[0] for the ungate). `--reachable-scan` runs at every checkpoint so we get the full state timeline. Wedge-immune -- every read/write target is proven reachable from RUN O.

**State as of 2026-07-21 (post RUN S):**
- Step 5.8.a (`--axi2af-naked-apply` -> axi_base): 14 STUCK (bit-31 sets at axi_base +0x00..+0x34 except +0x38, and +0x3c) / 42 SKIP-NOOP (pre-value already matched target, so no write needed) / 2 NO-OP (@ axi_base+0x38 val=0x80000016 and +0x40 val=0x8000000c) / 0 PARTIAL / 0 READ_FAIL, 58 of 58 entries touched. Naked-write path is broadly viable for axi_base; adjacent entry #15 @ +0x3c (identical mask 0x800000ff, identical value 0x8000000c to entry #16) DID stick, so the two NO-OP failures are offset-specific, not value-specific. 42 SKIP-NOOPs means most axi_base tunables were already at target from ADT-side pre-programming -- nothing to write.
- Step 5.8.b (`--pcieclkgen-naked-apply-to=axi_sub5_base`, 1 entry): STUCK. `#0 @ 0x495046200 mask=0x3e0 value=0x220 pre=0x00000a01 new=0x00000a21 post=0x00000a21`. Bits 5..9 landed at axi_sub5_base+0 exactly as intended.
- Step 6.g STILL wedges on the same offset: `RAISED at #0 (shared) 0x497040038: UartTimeout: Expected 1 bytes, got 0 bytes`. `phy_ip_base+0x38` unreachable identically to RUNs A..R. The naked axi2af apply + naked pcieclkgen apply are individually **necessary but not sufficient**.
- **Signal that jumps out:** phy_ip stalls at offset 0x38. Of the 58 axi2af entries applied at axi_base, the ONLY two that refused bit-31 sets were axi_base+0x38 and axi_base+0x40. Same offset alignment (0x38) between "axi_base register we can't set bit 31 on" and "phy_ip register that AXI-stalls". Either a genuine aperture / enable-bit correlation or coincidence -- TBD via RUN 3+ if RUN 1 doesn't unblock.
- **Persistence question:** RUN R had set `axi_base+0` bit 31 (0x8000001c) naked. RUN S entry #0 was SKIP-NOOP with pre=0x8000001c (bit 31 was still set at the start of Phase F). But the tail of the RUN S log shows the post-6.f reachable-scan reads back `axi_base+0=0x0000001c` (bit 31 gone). Something between step 5.8.a and step 6.f (steps 6.a-6.f are all writes to phy_shared) has a side-effect that clears bit 31 at axi_base+0. Diagnosing this is the RUN 2 hypothesis if RUN 1 fails.
- Next iteration ladder (starting 2026-07-21) restarts numeric at RUN 1 and continues 1..N until PCIe trains. Letters A..S remain as dispatcher cases for historical reproducibility.

**State as of 2026-07-21 (RUN 1 dispatched):**
- RUN 1 dispatched via `Scripts/m1n1/perstn-run.sh 1`. Flags: RUN S baseline + `--phycmn-early`. Only delta vs RUN S: `--phycmn-early` (mask32(phy_common+0, MODE=1) after step 6.f, before phy_ip).
- Hypothesis: CLK_MODE=ON combined with the naked axi2af + naked pcieclkgen state that RUN S already established is the phy_ip ungate. This co-application has NEVER been tried: RUN I did CLK_MODE=ON alone (silent-stalled at phy_ip); RUN S did the naked applies alone (silent-stalled at phy_ip); RUN K bundled RUN I + RUN J (extra-tunables via broken applicator, so cio3pllcore/pcieclkgen didn't land) and reverted the fault-mode change. RUN 1 uses the RUN S naked path (which DOES land) plus RUN I's CLK_MODE=ON.
- Wedge-immune diag posture: `--phy-ip-diag-at=none` skips phy_ip reads; `--reachable-scan` snapshots rc/phy_common/phy_shared/axi/axi_sub5/axi_sub6 windows at every Phase F checkpoint including the new `post-7.phycmn-early` slot. Even if step 6.g still wedges, the reachable-scan diff between post-5.8.b (RUN S post-state) and post-7.phycmn-early (new snapshot) tells us exactly what CLK_MODE=ON toggled after the naked applies had landed.
- Success criterion: step 6.g stops wedging at `phy_ip_base+0x38`.
- Branching plan sketch for RUNs 2..4 (decided in advance from findings.md open questions, executed only if RUN 1 still wedges):
  - **RUN 2:** insert mid-step reachable-scans at post-6.a, post-6.b, post-6.c, post-6.d, post-6.e alongside the existing post-6.b/6.d/6.f checkpoints, focused on axi_base+0..0x40. Diagnoses which of steps 6.a-6.f's phy_shared writes side-effect the axi_base+0 bit 31 clear observed between RUN S's 5.8.a and 6.f. Requires perstn.py change (new checkpoint slots + reachable-scan gating).
  - **RUN 3:** probe write behavior at axi_base+0x38 and +0x40 directly: try mask=0xffffffff, try bit-31-only write with different values, try writes to adjacent offsets, try after CLK_MODE=ON. Distinguishes R/O from write-locked-until-upstream-unlock. Requires perstn.py change (new probe function).
  - **RUN 4:** apply apcie-pcieclkgen-tunables to BOTH axi_sub5_base+0 AND rc_base+0 (its ADT-declared reg_idx=1 target). Tests whether pcieclkgen needs dual application. Dispatcher-only change (new flag combo -- the naked apply function already supports arbitrary block attrs).

**State as of 2026-07-21 (post RUN 1):**
- **RUN 1 hypothesis FALSIFIED.** Step 6.g still wedges at `phy_ip_base+0x38` (entry #0, `UartTimeout: Expected 1 bytes, got 0 bytes`), identical failure address and error class as every RUN A..S. CLK_MODE=ON does NOT unblock phy_ip even when combined with the naked axi2af + naked pcieclkgen state RUN S established. All three combinations tried (CLK_MODE=ON alone in RUN I; naked applies alone in RUN S; both together in RUN 1) silent-stall at the same address.
- The `--phycmn-early` write itself LANDED cleanly: `mask32(phy_common+0, MODE_MASK=0x3, MODE_ON=0x1)` returned `0x80300001`, guard `exc_count delta = 0`. `phy_common+0` transitions `0x80300000 -> 0x80300001` and stays set through every subsequent reachable-scan up to the wedge point. So the failure is not "the write didn't take" -- it's "the write took, and it wasn't the missing piece."
- RUN S's naked-apply write results reproduced bit-for-bit (deterministic across boots): axi2af 14 STUCK / 2 NO-OP at `+0x38`, `+0x40` / 42 SKIP-NOOP, and pcieclkgen 1 STUCK at `axi_sub5_base+0` (`0x00000a01 -> 0x00000a21`). Same-mask same-value adjacent axi2af entry (`+0x3c`) stuck, confirming the two NO-OPs at `+0x38` / `+0x40` are offset-specific not value-specific.
- **New finding: axi_base+0 bit-31 clear window tightened from "5.8.a..6.f" to "naked-extra axi2af entries #1..#57".** Reachable-scan at post-5.75.naked-write-test shows `axi_base+0 = 0x8000001c` (bit 31 stuck from the naked-write-test's final write). The naked-extra axi2af entry #0 pre-read at the start of 5.8.a still sees `0x8000001c` (SKIP-NOOP -- no write attempted). Then post-5.8.a rescan shows `0x0000001c` (bit 31 gone). Reads alone during post-5.75's full window scan did NOT clear it; only the subsequent 57 writes to other axi_base offsets did. Three candidate mechanisms remain open: (a) a specific entry's write side-effects `+0` bit 31, (b) any AXI write into the block clears the latch, (c) time-based decay. Distinguishable by mid-apply reachable-scan (RUN 2 candidate B).
- Consolidated interpretation across A..1: `phy_ip+0x38` is the persistent wedge point across 20+ boots. Nothing tried has moved it -- not T8140-replay, not CLK_MODE=ON (alone or with naked applies), not T8122's 6.i-early (RUN L), not T81XX PHYIF_CTRL_RUN (RUN G), not cio3pllcore/pcieclkgen via broken applicator (RUN J) or via naked apply (RUN S/1), not axi2af via naked apply (RUN S/1). Remaining live hypotheses: (i) phy_ip is decode-locked in both directions pending an upstream unlock not yet identified; (ii) `axi_base+0x38` / `+0x40` are phy_ip aperture-enable bits and are upstream-write-locked from bit-31 sets; (iii) T8122's post-phy-tunables writes T8140 skips (`poll(phy_shared+0x8, 1, 1)` + `set32(phy_shared+0, 0x200)`, pcie.c:543-551) matter on t8132 (untested by any prior RUN).
- Next iteration: RUN 2 candidates enumerated in `Scripts/m1n1/logs/1/findings.md` (A: phy_ip write-probe from RUN 1 state; B: mid-apply bit-31 bisection; C: axi_base+0x38/+0x40 write-lock probe; D: T8122 shared-init post writes). Selection deferred to the next dispatcher-writing session.

**State as of 2026-07-21 (RUN 2 dispatched):**
- RUN 2 selection: **new candidate E** (proposed after research), NOT one of findings.md's A/B/C/D. Chosen because A/B/C/D are all diagnostic follow-ups on the current failure signature; E closes a *config gap* whose absence is a plausible root cause of that signature. If E fails, A/B/C/D remain and are informed by E's outcome.
- RUN 2 dispatched via `Scripts/m1n1/perstn-run.sh 2`. Flags: RUN 1 baseline + `--cio3pllcore-naked-apply-to=axi_sub5_base`.
- Config gap closed: the ADT property `apcie-cio3pllcore-tunables` is a t8132-specific tunable that `m1n1/src/pcie.c` has NO code for on any codepath (verified: `grep -rn cio3 /home/ahmed/Projects/C/embedded/m1n1/src/` returns zero PCIe hits). It has 7 entries. RUN R probed pre-values of entries #0..#3 on axi_sub5 (offsets `0x00, 0x24, 0x28, 0x38`) and reported them SKIP-NOOP because sub5's pre-state already matches the target values -- then stopped, marking cio3pllcore "already applied in reset state." But RUN R's scan window only covered `0..0x40`; entries #4 (`+0x4c` mask `0xff` value `0x94`), #5 (`+0xe8` mask `0xe0000` value `0x20000`), and #6 (`+0x100` mask `0xffffff` value `0xb40b4`) sit OUTSIDE that window and have never been applied on any base. Entry #6 in particular is a 24-bit config write -- the substantial PLL analog config.
- Hypothesis: cio3pllcore #4-#6 configure the CIO3 PLL analog block (parallel to `kboot_atc.c`'s `tunable_CIO3PLL_CORE` at ATC offset `0x2A00`; CIO3PLL is a known Apple high-speed-I/O reference PLL shared between USB4 and PCIe). Without those writes, phy_ip has no working reference clock, and any AXI access to `phy_ip_base+0x38` (first PLL tunable target) stalls silently because the analog block doesn't respond.
- Wedge-immune diag posture: `--phy-ip-diag-at=none` skips phy_ip reads; `--reachable-scan` snapshots every reachable block at every Phase F checkpoint, including the new `post-5.8.c.cio3pllcore-naked-apply` slot. The axi_sub5/sub6 scan windows were **widened from `+0..+0x40` to `+0..+0x100`** so entries #4/#5/#6 pre/post state is captured at every checkpoint. Even on failure, cross-checkpoint diff of post-5.8.b (RUN S/1 post-state) vs post-5.8.c (new snapshot) tells us exactly which of #4-#6 landed and which no-op'd.
- Success criterion: step 6.g stops wedging at `phy_ip_base+0x38`.
- Interpretation matrix for RUN 2 outcome:
  - **6.g clean:** cio3pllcore #4-#6 was the missing PLL config. Proceed to RUN 3 = full pipeline through 6.g/6.h phy-ip tunables + Phase G per-port bring-up. LTSSM should now train.
  - **6.g wedges, #4-#6 STUCK on sub5:** PLL is configured but something ELSE gates phy_ip. RUN 3 = findings.md candidate A (phy_ip write-probe: does phy_ip decode POSTED writes now?) or B (mid-apply bit-31 bisection).
  - **6.g wedges, #4-#6 NO-OP on sub5:** axi_sub5 is the wrong target for these entries. RUN 3 = re-dispatch RUN 2 with `--cio3pllcore-naked-apply-to=axi_sub6_base` (sub6's pre-state is all-zero on 0..0x40, so its offsets would visibly STICK if it's the right block).
  - **Any of #4-#6 raises SYNC / delta != 0:** we hit a live register that actively responds. Highly informative -- narrows the block on offset. RUN 3 focused on the offending offset.
- Fallback if RUN 2 does not move the wedge: pause local iteration and read upstream Linux Asahi `drivers/pci/controller/dwc/pcie-apple.c` + PHY driver source for T8140/T8132 code paths. Look for a mailbox / indirect-access / unlock sequence not modelled in perstn.py. All local hypotheses depend on m1n1's model of the init sequence being complete; Linux's driver may reveal a structural difference (e.g., PLL enable via mailbox rather than direct AXI writes).

**State as of 2026-07-21 (post RUN 2 + Asahi source hunt):**
- RUN 2 wedged at `phy_ip_base+0x38` identical to every prior RUN. cio3pllcore-naked-apply-to=axi_sub5_base with entries #4-#6 did NOT unblock. Same failure address (`0x497040038`), same failure class (`UartTimeout`, m1n1 UART-froze).
- Completed a full read-only sweep of upstream Linux (asahi-wip clone at `/home/ahmed/Projects/C/embedded/untouched_asahi_linux/`), the m1n1 fork's t8132 commit lineage, the DT binding, the t8132 pmgr/j773g dtsi chain, the `atc.c` CIO3PLL_CORE pattern, and the GitHub asahi-soc/for-next branch. lore.kernel.org is Anubis-protected and inaccessible via WebFetch; the local `page.html` snapshot has zero t8132-pcie threads. Full details captured in [[ref-asahi-t8132-pcie]].
- **Biggest single finding:** in `atc.c` the CIO3PLL registers sit at `regs.core + 0x2a00`, with `CIO3PLL_DCO_NCTRL` at `+0x2a38` (DCO calibration efuse). If PCIe's `phy_ip` window is laid out the same way (CIO3PLL block at `phy_ip+0`), then `phy_ip+0x38` is the DCO NCTRL register we've been trying to write BEFORE turning on the CIO3PLL clock. `atc.c:1778-1779` explicitly fires `CIO3PLL_CLK_CTRL PCLK_EN` then `REFCLK_EN` before touching any lane / calibration register. Our replay does neither.
- Rules out: hypothesis that `APCIE_PHY_SW` gate is left off. `logs/1/nic-runtime.txt:399-491` shows Phase D poke drives `APCIE_SYS_ST` then `APCIE_PHY_SW` to actual=0xf, and Phase E confirms. The t8132 phy_sw gate is NOT `apple,always-on` (unlike t8122/t6030 — see [[ref-asahi-t8132-pcie]] §4), so this was worth verifying but is not the blocker.
- Rules out: hypothesis that upstream Linux driver has a hidden init step we're missing. `pcie-apple.c:508-542` `apple_pcie_setup_refclk` only twiddles four bits in `port->phy + PHY_LANE_CFG` (REFCLK0/1 REQ/ACK handshake, REFCLKEN). It never touches PHY analog offsets ≥ 0x08. The driver has no compat for t8132 and never had. Everything phy-analog is expected pre-programmed by the boot chain.
- Chosen RUN 3 candidate: **RUN 3a — `--cio3pllcore-naked-apply-to=phy_common_base`** (Section 8.1 of [[ref-asahi-t8132-pcie]]). Rationale: `atc.c:882-884` applies common tunables to `regs.core`, and `phy_common_base` (0x497004000 in our recon) is the direct PCIe analog. RUN R found entries #0-#3 were SKIP-NOOP against axi_sub5 — that only says sub5's pre-state happens to match, not that sub5 is the right target. Wedge-immune posture: `--phy-ip-diag-at=none`, widen reachable-scan `phy_common+0..+0x2c00` to cover the potential CIO3PLL range at +0x2a00, snapshot pre vs post 5.8.c. Even if 6.g still wedges, the differential data identifies the block. Fall-through candidates queued in [[ref-asahi-t8132-pcie]] §8.2-8.5 (phy_ip write-only replay; T8122 shared-init post; atc.c-style CIO3PLL enable pair; findings.md B/C).

**State as of 2026-07-21 (post RUN 3):**
- **RUN 3 hypothesis FALSIFIED.** `--cio3pllcore-naked-apply-to=phy_common_base` on the RUN 2 baseline still wedges at `phy_ip+0x38` (`0x497040038`, `UartTimeout`) identically to every RUN A..2. Full RUN 3 findings + register-level diff tables in `Scripts/m1n1/logs/3/findings.md`.
- **atc.c CIO3PLL analogy does not extend to phy_common on t8132.** Widened reachable-scan of `phy_common +0x2a00..+0x2c00` (128 words) reads all zeros — no CIO3PLL_CLK_CTRL @ +0x2a00, no DCO_NCTRL @ +0x2a38, no analog block at all in that offset range. The atc.c layout [[ref-asahi-t8132-pcie]] §3c relies on does not port to PCIe's phy_common the way §8.1 hoped.
- **cio3pllcore apply on phy_common: 0 STUCK / 1 PARTIAL / 5 NO-OP / 1 SKIP-NOOP.** Entry #0 (phy_common+0) PARTIAL because only CLK_MODE bit 0 stuck (redundant with `--phycmn-early`); bits 9, 11 rejected. Entries #1-#6 all read back zero after write, no SError / SYNC. phy_common at these offsets is R/O or write-drop.
- **Target-block search for cio3pllcore is EXHAUSTED across all four candidate blocks:** `sub5` (SKIP-NOOP, iBoot pre-programmed — RUN 2), `sub6` (PARTIAL on #0 first-touch — RUN R), `rc_base` (NO-OP on #0/#1/#2 — RUN Q/R), `phy_common` (PARTIAL on #0 only, NO-OP on rest — RUN 3). No target-block permutation remains that has a meaningful chance of moving the wedge.
- **cio3pllcore's true home is `axi_sub5`.** Reachable-scan @ post-5.8.c at `Scripts/m1n1/logs/3/nic-runtime.txt:3413-3459` shows heavy iBoot pre-programming (`sub5+0x30=0x1e814`, `+0x34=0x80000000`, `+0x4c=0x1f800094` (cio3pllcore #4 target 0x94 in low byte), `+0x60=0xec040018`, `+0x64=0x0b51991e`, `+0x78..+0x8c` all populated). iBoot has already programmed sub5 to the cio3pllcore target values; the ADT tunables describe writes iBoot performs, not writes m1n1 must repeat.
- **New concern: `--naked-write-test`'s sub5+0 first-touch probe may be DESTRUCTIVE.** `nic-runtime.txt:1573`: `pre=0x00081f55 wrote=0x00000a01 post=0x00000a01 [STUCK]`. iBoot's `sub5+0 = 0x00081f55` was clobbered to `0x00000a01`, losing bits 2, 4, 6, 8, 12 (all outside cio3pllcore's target mask). Every RUN S / 1 / 2 / 3 has been running this destructive probe before Phase F starts. If any of those lost bits is a PLL enable or reference-clock select, we've been gating our own PLL off for the last five RUNs. Untested: RUN S baseline WITHOUT `--naked-write-test`.
- **Consolidated interpretation across A..3:** `phy_ip+0x38` remains the persistent wedge address for 21+ boots. Nothing tried has moved it. Remaining live hypotheses: (i) `--naked-write-test`'s sub5+0 clobber has been breaking iBoot's PLL config all along (highest-priority experiment; dispatcher-only); (ii) phy_ip is decode-locked in reads only — POSTED writes may land (RUN P tested this from a weaker state); (iii) T8122 shared-init post writes T8140 skips (`poll(phy_shared+0x8, 1, 1)` + `set32(phy_shared+0, 0x200)` at pcie.c:543-551) are required on t8132; (iv) the C-side reaches LTSSM BUSY (per commit `6b277bc`) via a path our Python replay diverges from — barrier/ordering, silent write-drop, or timing.
- **Local naked-apply target-block brute-forcing is retired.** Continuing with RUN 4, 5, 6, ..., 99 that keep permuting `--cio3pllcore-naked-apply-to=<attr>` is not productive — the answer is not in that search space. The next productive experiments are upstream (drop destructive probes) or lateral (phy_ip write-decode, T8122 post writes).

**Proposed RUN 4 plan (2026-07-21):**

Three single-variable candidates ranked; RUN 4 = candidate A per the priority argument below. B and C are RUN 5 / RUN 6 dispatches unless A opens a new lever.

| Candidate | Change vs RUN 1 baseline (RUN S + `--phycmn-early`) | Hypothesis | perstn.py change? | Cost |
|---|---|---|---|---|
| **A** | REMOVE `--naked-write-test` (keep `--axi2af-naked-apply`, `--pcieclkgen-naked-apply-to=axi_sub5_base`, `--phycmn-early`, `--reachable-scan`, `--phy-ip-diag-at=none`) | The naked-write-test's sub5+0 clobber destroys iBoot's `0x00081f55` PLL control word (bits 2/4/6/8/12 outside cio3pllcore's mask). Without those bits, the PLL never reaches lock and phy_ip AXI-stalls on first touch. | No — dispatcher-only | Cheap |
| **B** | Add `--phy-ip-write-probe` + `--phy-ip-diag-at=post-7.phycmn-early` (keep everything from RUN 1) | Does phy_ip decode POSTED writes to `+0x38` in the RUN 1 state (naked applies landed, CLK_MODE=ON)? RUN P tested from broken-applicator baseline; this state has never been probed. Binary result: WROTE opens a "sequence phy-ip-pll via naked write32" strategy; STALL/SYNC forces upstream aperture search. | No — flag exists | Cheap |
| **C** | Add `--t8122-shared-post` running `poll(phy_shared+0x8, 1, 1, 250000)` + `set32(phy_shared+0, 0x200)` between step 6.f and 6.g (pcie.c:543-551) | Do the T8122 shared-init post writes T8140 skips gate phy_ip on t8132? Untested by any prior RUN. `dc25f2f` warned this trips SError on the C-side, but individual guarded writes isolate the fault. | Yes — new step + CLI flag | Medium |

**Selection: RUN 4 = candidate A.** Reasoning:
1. It is the ONLY candidate that questions the validity of the last 5 RUNs' baseline. If A succeeds, RUNs S / 1 / 2 / 3 were all confounded by the destructive probe. If A fails, the destructive-probe hypothesis is definitively ruled out and RUN 5 (candidate B) proceeds from a cleaner logical position.
2. It is dispatcher-only — zero perstn.py changes, one new `case 4)` in `perstn-run.sh` with the flag omission. Fastest possible iteration.
3. It touches the fewest lines of new instrumentation, minimizing the chance of adding a new source of state mutation while investigating whether prior mutation was the problem.
4. Success criterion: `phy_ip+0x38` stops wedging at 6.g. Failure informs RUN 5 selection between B and C.

RUN 4 wedge-immune posture: `--phy-ip-diag-at=none` + `--reachable-scan` retained. Even on failure, cross-checkpoint diff between RUN 3 (with the clobber) and RUN 4 (without) at post-5.75 and post-5.8.b reveals which bits at `sub5+0` iBoot originally set, which the clobber removed, and whether their absence propagates to any observable state elsewhere. Note: WITHOUT `--naked-write-test`, `axi_sub5_reachable` / `axi_sub6_reachable` first-touch flags never flip, so the sub5/sub6 reachable-scan windows will be SKIPPED at every checkpoint. That's an intentional trade — we lose sub5/sub6 visibility to preserve iBoot state. A follow-up RUN 4b (if 4a still wedges) can add a *non-destructive* first-touch probe (READ-only, no write) to re-enable the scan windows without destroying iBoot state.

**Correction to RUN 4 plan (2026-07-21, during dispatcher implementation):** Reviewing `perstn.py` before writing `case 4)` surfaced that `--pcieclkgen-naked-apply-to=axi_sub5_base` is gated at `perstn.py:3366-3370` behind the `axi_sub5_reachable` flag, which is ONLY flipped True by `probe_naked_write_test`'s pre-read stage at `perstn.py:2085-2089`. Simply dropping `--naked-write-test` (as the candidate A table above describes) would silently SKIP step 5.8.b's pcieclkgen apply — changing two variables vs RUN 1, not one, and destroying the single-variable-delta property the plan explicitly wanted.

**Implemented candidate A3 instead.** A new `--naked-write-test-readonly` flag runs `probe_naked_write_test` with `write=False`: pre-read + first-touch-flag flip proceed unchanged, the destructive full-word write + post-read stages are skipped. Preserves iBoot's `sub5+0 = 0x00081f55` (only pcieclkgen's mask-0x3e0 bits 5-9 change) AND keeps the sub5/sub6 reachable-scan windows enabled at every Phase F checkpoint. Single-variable delta vs RUN 1 restored: only the destructive full-word write is gone.

**A3 supersedes the RUN 4b hypothesized in the paragraph above** — the read-only first-touch probe it proposed is now RUN 4 itself, saving a boot iteration. Success/failure interpretation matrix from candidate A applies unchanged, with one added diagnostic branch: if pre-read STALLs on sub5+0, sub5 is unreachable without a prior write (fabric quirk); RUN 5 would then revert to `--naked-write-test` and reconsider. If the mask-RMW-only end-state of sub5+0 (predicted ~`0x00081f75` = 0x81f55 with pcieclkgen bits 5+9 also set) is what we observe post-6.f, that in itself is new signal on which iBoot bits the mask-RMW path preserves.

Implementation lives in `Scripts/m1n1/perstn.py` (probe signature + argparse + mutual-exclusion validation, ~35 lines) and `Scripts/m1n1/perstn-run.sh` (`case 4)` + header comment + usage extension, ~65 lines). Run with `./Scripts/m1n1/perstn-run.sh 4`.

**State as of 2026-07-21 (post RUN 4):**
- **RUN 4 hypothesis FALSIFIED.** Step 6.g still wedges at `phy_ip_base+0x38` (entry #0, `UartTimeout`, m1n1 UART-froze) identically to every RUN A..3. The destructive `--naked-write-test` sub5+0 clobber was NOT the confounding variable for RUNs S/1/2/3. Full findings + register-level diff tables in `Scripts/m1n1/logs/4/findings.md`.
- **`--naked-write-test-readonly` refactor worked as designed.** `axi_sub5_reachable` and `axi_sub6_reachable` first-touch flags flipped True at the pre-read stage (nic-runtime.txt:1571-1580), enabling downstream reachable-scan windows AND step 5.8.b's pcieclkgen naked apply. `sub5+0 = 0x00081f55` (iBoot value) was PRESERVED through post-5.75 and post-5.8.a — direct proof the READ-ONLY variant satisfies the reachability gate without destroying iBoot state.
- **pcieclkgen naked apply landed cleanly, but STILL clobbers 2 iBoot bits.** `#0 @ 0x495046200 mask=0x3e0 value=0x220 pre=0x00081f55 new=0x00081e35 post=0x00081e35 [STUCK]`. Bit math: iBoot bits 6, 8 fall inside the mask and get cleared; bit 5 gets set (from `value`); the eight iBoot bits outside the mask (0, 2, 4, 9, 10, 11, 12, 19) survive. Compare RUN 3 where the destructive probe left sub5+0 at `0x00000a01` (only 3 bits set) — RUN 4 preserved 8 of 10 iBoot bits and the wedge STILL fires at the same address.
- **sub5+0 persistence: `0x00081e35` stable across post-5.8.b through post-6.f** (six checkpoints). Nothing between pcieclkgen apply and 6.g mutates it — not phy_shared CLK0/CLK1 ACK writes, not CLK_MODE=ON, not the T8140 phy_shared+4 marker. The mask-RMW state is what phy_ip sees at 6.g.
- **axi2af naked apply: 15 STUCK / 2 NO-OP / 41 SKIP-NOOP** (nic-runtime.txt:2200-2205). Same offset-specific NO-OPs at `axi_base+0x38` and `+0x40` as RUNs S / 1 — both refuse bit-31 sets while adjacent `+0x3c` (same mask + same value) STUCK. The `+0x38` low-nibble alignment with the `phy_ip+0x38` wedge address is still suspicious.
- **axi_base+0 bit-31 clear window mechanism is INDEPENDENT of `--naked-write-test`.** RUN 4 skipped the destructive write to axi_base+0 (READ-ONLY probe only pre-reads), yet bit 31 still gets set by axi2af entry #0 (post-write readback `STUCK`) and CLEARED again by the time post-5.8.a's reachable-scan runs. Confirms mechanism is either "clear-on-any-subsequent-write in the block" or "decay timer," not "the naked-write-test's final write clobbered it."
- **Consolidated interpretation across A..4:** `phy_ip+0x38` is now the persistent wedge address across 22+ boots. Nothing tried has moved it. The remaining live hypotheses are: (i) pcieclkgen's mask-RMW clobbers iBoot bits 6 or 8 which are a PLL enable — a bit-5-only variant would test this; (ii) phy_ip is decode-locked in reads only, POSTED writes may still land — RUN P tested from a weaker state, RUN 4 has cleanest state ever; (iii) T8122 shared-init post writes (`poll phy_shared+0x8`, `set32 phy_shared+0 0x200`, pcie.c:543-551) — untested; (iv) C-side barrier/DSB/ISB ordering the Python replay does not model.

**Proposed RUN 5 plan (2026-07-21):**

Three single-variable candidates ranked; RUN 5 = candidate A per the priority argument below.

| Candidate | Change vs RUN 4 baseline | Hypothesis | perstn.py change? | Cost |
|---|---|---|---|---|
| **A** | Add `--phy-ip-write-probe`. Change `--phy-ip-diag-at=none` to `--phy-ip-diag-at=post-7.phycmn-early`. | phy_ip decodes POSTED writes even when reads AXI-stall. RUN 4's state is the cleanest ever probed (naked axi2af + naked pcieclkgen at sub5+0 landed, CLK_MODE=ON). RUN P tested from a broken-applicator baseline where NO naked applies had landed — this state has never been probed for write-decode. Binary result: WROTE opens a "naked write32-only replay of apcie-phy-ip-pll-tunables" strategy (29 shared entries). STALL/SYNC definitively closes off write-only strategies. | No — dispatcher-only, flag already exists | Cheap |
| **B** | Drop `--pcieclkgen-naked-apply-to=axi_sub5_base`, OR replace pcieclkgen's mask-RMW with a bit-5-only set32 that preserves iBoot bits 6, 8 | pcieclkgen's clobbering of iBoot bits 6, 8 is the missing config. Drop version has confound: iBoot's bit 5 stays 0 (which pcieclkgen wants set) — some part of pcieclkgen is needed. Narrow-mask variant (set bit 5 without clearing bits 6, 8) preserves more iBoot state and tests whether bit-5-set alone is enough. | Drop: dispatcher-only. Narrow: ~15 lines (new `--pcieclkgen-set5-only` mode) | Cheap-medium |
| **C** | Add `--t8122-shared-post` running `poll(phy_shared+0x8, 1, 1, 250000)` + `set32(phy_shared+0, 0x200)` between step 6.f and 6.g (pcie.c:543-551) | T8122's post-phy-tunables writes T8140 skips gate phy_ip on t8132. `dc25f2f` warned this trips SError on the C side; guarded Python isolates the fault to a specific write. Untested by any prior RUN. | Yes — new step + CLI flag (~40 lines) | Medium |

**Selection: RUN 5 = candidate A.** Reasoning:
1. Only candidate that hard-forks the next 3-5 RUNs on a binary result. WROTE and STALL/SYNC lead to fundamentally different next-step strategies (naked-write replay of pll tunables vs upstream aperture search).
2. Dispatcher-only. Fastest iteration. Flag already exists in perstn.py argparse.
3. RUN 4's state (all naked applies landed, CLK_MODE=ON, iBoot bits mostly preserved) is the cleanest starting point for a write-decode probe we have ever had. Any WROTE result from this state is a strong signal.
4. Does not rule out B or C — they are queued for RUN 6 / RUN 7 depending on RUN 5's outcome.

RUN 5 dispatch flags:
```
--no-pcie-init --preinit-probe --pmgr-enable --pmgr-per-port --gate-poke
--phy-ip-probe --t8140-replay --phy-ip-diag --fuse-recon
--naked-write-test-readonly --axi2af-naked-apply
--pcieclkgen-naked-apply-to=axi_sub5_base --reachable-scan --phycmn-early
--phy-ip-write-probe --phy-ip-diag-at=post-7.phycmn-early
```

Success criterion: the `--phy-ip-write-probe`'s posted `write32(phy_ip_base + 0x38, 0)` at post-7.phycmn-early lands cleanly (WROTE, guard delta = 0). Failure informs whether phy_ip is decode-locked in both directions or only in reads.

Interpretation matrix for RUN 5:
- **WROTE (probe returns cleanly, guard delta = 0)** → phy_ip decodes posted writes in the RUN 4 state. RUN 6 = naked-write32 replay of the 29 apcie-phy-ip-pll-tunables shared entries (no readback, no RMW). If those land, RUN 7 = same for apcie-phy-ip-auspma-tunables. This is a full new attack surface.
- **STALL (UartTimeout as at 6.g reads)** → phy_ip is bidirectionally decode-locked from the current fabric state. Refocuses on B (pcieclkgen bit-5-only), C (T8122 shared-init post), or upstream unlock hunt.
- **SYNC (Exception: SYNC — the fault mode RUN J saw on reads)** → writes decode but hit a live register that actively responds with an SError. Highly informative — pins the fault mode on writes at `phy_ip+0x38` specifically.
- **Any other outcome (e.g. probe raises + subsequent flow continues)** → new signal; interpret ad hoc.

**State as of 2026-07-21 (post RUN 5):**
- **RUN 5 hypothesis FALSIFIED (STALL branch).** The posted `write32(phy_ip_base + 0x38, 0)` at `post-7.phycmn-early` bus-hung m1n1 exactly like every prior read wedge at 6.g. `Scripts/m1n1/logs/5/nic-runtime.txt:4422` ends mid-line with no post-write log — UART froze the moment the write was issued. `run.log` final flush was `phaseF.diag.post-7.phycmn-early.phy-ip-write-enter` at 336398 bytes = exact file size. phy_ip is **bidirectionally decode-locked** from the RUN 4 baseline (naked axi2af + naked pcieclkgen landed + CLK_MODE=ON). Full findings in `Scripts/m1n1/logs/5/findings.md`.
- **Write-only strategy branch eliminated.** WROTE was the outcome that would have opened the naked-write32 replay of 29 `apcie-phy-ip-pll-tunables` shared entries. STALL closes it definitively — writes AXI-stall the same as reads. Fabric never routes phy_ip transactions to a live slave from this state, so we don't even get an SError (unlike RUN J with `--extra-tunables` which reached `Exception: SYNC` from a weaker baseline).
- **Phase F progressed further than any prior RUN.** For the first time we cleanly walked steps 6.a-6.f + step 7 (via `--phycmn-early`) and captured a full reachable-scan at `post-7.phycmn-early` (rc / phy_common / phy_shared / axi / axi_sub5 / axi_sub6, all offsets guarded delta=0). New differential data never previously captured at this checkpoint.
- **THE key finding: `phy_shared+0x8 = 0x00000000` at post-7.phycmn-early** (`nic-runtime.txt:4215`). Bit 0 is CLEAR. pcie.c:543-551 has T8122/T602X/T6031 run `poll32(phy_shared+8, 1, 1, 250000)` here — that poll would time out at 250 ms on t8132 from this state. T8140 skips it entirely. Directly after the poll, T8122 does `set32(phy_shared+0, 0x200)` (bit 9); T602X does `set32(phy_shared+0, 0x300)` (bits 8+9). Currently `phy_shared+0 = 0xf3c0301f` — bits 8, 9 both CLEAR. Neither of these post-writes has ever run in our replay.
- **Hypothesis for RUN 6:** bit 9 of `phy_shared+0` (`0x200`) is a phy_ip decode-enable gate that T8140 doesn't have but T8122/T8132 do. Setting `phy_shared+0 |= 0x200` after step 7 unlocks phy_ip decode. Skip the C-side poll (it would just timeout — bit 0 of `phy_shared+8` may become 1 only AFTER the bit-9 set, or may be a T8122-specific status bit that has no t8132 equivalent). Do the `set32` unconditionally. Novel attack surface — untested by RUNs A..5.
- **Consolidated interpretation across A..5:** phy_ip is bidirectionally decode-locked from every fabric state we have reached, including the strongest state (RUN 5: naked axi2af + naked pcieclkgen + CLK_MODE=ON + T8140 marker + CLK0/1 ACKed). Remaining live hypotheses: (i) **T8122 shared-init post writes** (RUN 6 = primary; novel, based on new `phy_shared+8=0` data); (ii) **pcieclkgen bit-5-only variant** preserving iBoot bits 6, 8 (RUN 7 fallback if RUN 6 fails); (iii) C-side barrier/DSB/ISB ordering (deferred until Python-side hypotheses exhaust).

**Proposed RUN 6 plan (2026-07-21):**

Three single-variable candidates ranked; RUN 6 = candidate A per the priority argument below.

| Candidate | Change vs RUN 4 baseline | Hypothesis | perstn.py change? | Cost |
|---|---|---|---|---|
| **A** | Add `--t8122-shared-post`. Injects between step 7 (phycmn-early) and step 6.g: pre-read `phy_shared+0x8` (log-only, skip poll since RUN 5 proved bit 0 clear), `set32(phy_shared+0, 0x200)`, post-read + verify, `diag("post-6.5.t8122-shared-post")`. Then step 6.g runs. | Bit 9 of `phy_shared+0` is a phy_ip decode-enable gate T8140 doesn't have but T8122/T8132 do. Setting it unlocks phy_ip. Novel attack surface — pcie.c:543-551 block never previously replayed by our script. | Yes — new step + `--t8122-shared-post` CLI flag + new `post-6.5.t8122-shared-post` diag checkpoint (~40 lines) | Medium |
| **B** | Replace `--pcieclkgen-naked-apply-to=axi_sub5_base` (mask 0x3e0 value 0x220) with `set32(axi_sub5+0, 0x20)` (bit 5 only) | pcieclkgen's clobber of iBoot bits 6, 8 (both inside mask, both cleared by mask-RMW) destroys a PLL enable. Bit-5-only set32 preserves them while still setting bit 5 (what pcieclkgen wants). | Yes — new `--pcieclkgen-set5-only` mode (~15 lines) | Cheap |
| **C** | Bundle A+B (both `--t8122-shared-post` AND `--pcieclkgen-set5-only`) | Both changes needed together | Yes — both above (~55 lines) | Medium |

**Selection: RUN 6 = candidate A.** Reasoning:
1. **RUN 5 produced a specific, actionable new data point** (`phy_shared+8 = 0`). Candidate A tests the direct hypothesis that data point suggests. Not testing it would waste the RUN 5 signal.
2. Novel attack surface. Neither the T8140 replay nor any prior RUN A..5 has ever written `phy_shared+0 |= 0x200`. Any outcome is high-signal.
3. B tests a variation of what RUN 4 already covered indirectly (iBoot bit preservation axis). RUN 4 preserved 8 of 10 iBoot bits and wedged. Preserving 2 more (bits 6, 8) is unlikely to be the axis — marginal information gain vs A.
4. C violates single-variable-delta discipline. If it works, we don't know whether A alone or B alone or both were required — need extra RUNs to bisect. Keeping A and B separate preserves discipline: RUN 7 = B alone if A fails.
5. Cost delta A vs B is small (~25 lines). Cost is not the deciding factor.

RUN 6 dispatch flags:
```
--no-pcie-init --preinit-probe --pmgr-enable --pmgr-per-port --gate-poke
--phy-ip-probe --t8140-replay --phy-ip-diag --fuse-recon
--naked-write-test-readonly --axi2af-naked-apply
--pcieclkgen-naked-apply-to=axi_sub5_base --reachable-scan --phycmn-early
--t8122-shared-post --phy-ip-diag-at=none
```

RUN 6 wedge-immune posture: `--phy-ip-diag-at=none` retained. No phy_ip probe.
Step 6.g runs (the FIRST phy_ip access from Python replay); if it succeeds,
Phase F continues to steps 6.h / 7 / 8 / 9 / 10 and we may complete shared-init
for the first time. If 6.g still wedges (same address, same UartTimeout), the
bit-9 hypothesis is ruled out and RUN 7 = candidate B.

Interpretation matrix for RUN 6:
- **6.g STOPS wedging** (any progress past `phy_ip_base+0x38`) → bit 9 hypothesis confirmed; `set32(phy_shared+0, 0x200)` is the missing decode-enable. Continue Phase F to see how far we get. If Phase F completes shared-init, run `p.pcie_init()` next. New chapter opens.
- **6.g wedges identically** at `phy_ip+0x38` → bit 9 not the ungate. RUN 7 = candidate B (`--pcieclkgen-set5-only`).
- **6.g wedges at a NEW address** or NEW error class → bit 9 partial ungate; something else still gates. New wedge address is high-signal.
- **SError somewhere INSIDE `--t8122-shared-post`** → the specific write triggering SError is isolated by our guarded Python (something the C-side `dc25f2f` warning could not pin down). Diagnostic gold; adjust the block accordingly.

**State as of 2026-07-21 (post RUN 6):**
- **RUN 6 hypothesis FALSIFIED.** `--t8122-shared-post` (`set32(phy_shared+0, 0x200)`) executed perfectly: `phy_shared+0` went from `0xf3c0301f` → `0xf3c0321f` (delta=`0x00000200`, bit 9 STUCK), guard delta=0, no SError. But step 6.g wedges IDENTICALLY to every RUN A..5: `RAISED at #0 (shared) 0x497040038: UartTimeout: Expected 1 bytes, got 0 bytes`. Full findings in `Scripts/m1n1/logs/6/findings.md`.
- **Bit 9 of `phy_shared+0` is NOT a phy_ip decode-enable gate.** We can set it, it stays set, phy_ip is still decode-locked. `phy_shared+0x8` bit 0 also unchanged post-write (0 → 0), so the T8122 poll-then-write handshake on t8132 needs some OTHER prerequisite we haven't identified. The pcie.c:543-551 block is not the missing step (at least the `set32(phy_shared+0, 0x200)` half of it).
- **Phase F now walks 8 consecutive steps cleanly** (6.a → 6.b → 6.c → 6.d → 6.e → 6.f → 7 → 6.5) with guard delta=0 on every intermediate access. Wedge is fabric-level AXI stall on first phy_ip access, not internal SError.
- **Post-6.5 reachable-scan captured** — cleanest post-6.5 state ever, `phy_shared+0=0xf3c0321f` (bit 9 now SET), `phy_common+0=0x80300001` (unchanged), everything else identical to post-7.phycmn-early. Direct proof the fabric route to phy_ip does NOT depend on bit 9 of phy_shared+0.
- **Consolidated interpretation across A..6:** phy_ip is bidirectionally decode-locked from every fabric state we've built, including the strongest (RUN 6: naked axi2af + naked pcieclkgen + CLK_MODE=ON + T8140 marker + CLK0/1 ACKed + phy_shared+0 bit 9 SET). Remaining live hypotheses: (i) **pcieclkgen mask-RMW clobbers iBoot bits 6, 8 which are PLL enables** (RUN 7 = primary, direct test); (ii) `set32(phy_shared+4, 0x10)` (T602X/T8122 bit 4 write, `--phy4-x10-early`, queued RUN 8); (iii) T602X-style `set32(phy_shared+0, 0x300)` (bits 8+9 vs RUN 6's bit 9 alone, queued RUN 9); (iv) C-side barrier/DSB/ISB ordering (deferred until Python-side hypotheses exhaust).

**Proposed RUN 7 plan (2026-07-21):**

Three single-variable candidates ranked; RUN 7 = candidate B per the priority argument below.

| Candidate | Change vs RUN 6 baseline | Hypothesis | perstn.py change? | Cost |
|---|---|---|---|---|
| **B (recommended)** | Add `--pcieclkgen-set5-only-to=axi_sub5_base`. Mutually exclusive with `--pcieclkgen-naked-apply-to`; replaces the mask-RMW (mask=0x3e0 val=0x220) with `set32(sub5+0, 0x20)`. Post-value = `0x00081f75` (all iBoot bits + bit 5), preserving iBoot bits 6, 8 that RUNs S/1/2/3/4/5/6 have all been clearing. | pcieclkgen's mask-RMW clears iBoot bits 6, 8 (both inside the 0x3e0 mask, neither set by value 0x220). If either is a PLL enable, phy_ip has no clock and fabric AXI-stalls on decode. Direct test of the PLL-enable-clobber axis. | Yes — new `--pcieclkgen-set5-only-to` flag + apply block (~25 lines) | Modest |
| **A (drop pcieclkgen)** | Drop `--pcieclkgen-naked-apply-to=axi_sub5_base` entirely. Skips step 5.8.b. sub5+0 stays at iBoot value `0x00081f55`. | pcieclkgen was the confounding damage — skipping it preserves all iBoot state. 2-variable delta (loses pcieclkgen apply AND preserves iBoot bits) so less clean. | No — dispatcher-only | Cheap but less interpretable |
| **C** | Add `--phy4-x10-early` (RUN L flag) on RUN 6 baseline. `set32(phy_shared+4, 0x10)` — the T602X/T8122 bit-4 write we skip. Direct parallel to RUN 6's bit-9 hypothesis but different offset. | Bit 4 of `phy_shared+4` is a T8122-only phy_ip decode-enable gate. RUN L tested it alone and wedged; combined with RUN 6 baseline is untested. | No — dispatcher-only | Cheap |

**Selection: RUN 7 = candidate B.** Reasoning:
1. **Strongest remaining single-variable hypothesis with hardware-plausible mechanism.** PCIe clock/gen block having enable bits at bits 6, 8 (positions inside the mask that pcieclkgen supplies value 0 for) is exactly the pattern where a tunable is meant to "select mode X while preserving hardware-configured enables Y" — and our applicator, by literally applying mask-RMW with the mask covering the enables, destroys them. iBoot pre-programmed `0x00081f55` with bits 6, 8 SET, our mask-RMW ALWAYS CLEARS them.
2. **Every RUN S/1/2/3/4/5/6 has been doing this exact clobber.** If bits 6, 8 are PLL enables, we've been turning the PLL off since RUN S. The wedge signature (fabric never routes = no clock arriving at phy_ip block) matches.
3. **Strict single-variable delta.** pcieclkgen's stated intent (setting bit 5) is preserved; only the destructive mask-RMW of bits 6, 8 is removed.
4. Cost is modest (~25 lines perstn.py).
5. If B works: bit 5 alone is enough, and pcieclkgen's mask-RMW has been the entire problem across 7 RUNs. New attack surface: audit every mask-RMW tunable for iBoot-bit clobbers.
6. If B fails: bits 6, 8 are not PLL enables. RUN 8 = candidate C (`--phy4-x10-early` on RUN 6/7 baseline).

RUN 7 dispatch flags:
```
--no-pcie-init --preinit-probe --pmgr-enable --pmgr-per-port --gate-poke
--phy-ip-probe --t8140-replay --phy-ip-diag --fuse-recon
--naked-write-test-readonly --axi2af-naked-apply
--pcieclkgen-set5-only-to=axi_sub5_base --reachable-scan --phycmn-early
--t8122-shared-post --phy-ip-diag-at=none
```

RUN 7 wedge-immune posture: `--phy-ip-diag-at=none` retained. Step 6.g runs. If bit 6 or bit 8 was the missing PLL enable, 6.g succeeds for the first time in 24+ boots and Phase F may complete.

Interpretation matrix for RUN 7:
- **6.g STOPS wedging** → pcieclkgen mask-RMW was the confounding variable across RUNs S/1/2/3/4/5/6. Continue Phase F. Possible first-ever completion of T8140 shared init.
- **6.g wedges same** at `phy_ip+0x38` → bits 6, 8 not PLL enables (or not enough on their own). RUN 8 = candidate C (`--phy4-x10-early`).
- **6.g wedges at NEW address** → partial ungate; the bit-5-only variant helped at least one earlier step. High-signal.
- **SError inside 5.8.b** → the bit-5-only write triggers a fault that mask-RMW's superset write did not. Very unusual; would indicate the write ordering / mask boundary matters at the fabric level.

**State as of 2026-07-21 (post RUN 7):**
- **RUN 7 hypothesis FALSIFIED.** `--pcieclkgen-set5-only-to=axi_sub5_base` executed perfectly: `axi_sub5+0` went `0x00081f55` → `0x00081f75` (bit 5 STUCK, delta=`0x20`, guard delta=0) with **iBoot bits 6, 8 preserved end-to-end for the first time since RUN S** — and held through every later reachable-scan checkpoint. Yet step 6.g wedged IDENTICALLY: `RAISED at #0 (shared) 0x497040038: UartTimeout: Expected 1 bytes, got 0 bytes` (`Scripts/m1n1/logs/7/nic-runtime.txt:5295`). Full findings in `Scripts/m1n1/logs/7/findings.md`.
- **Bits 6, 8 of `axi_sub5+0` are NOT phy_ip PLL/clock enables** (or not sufficient). The sub5+0 iBoot-bit-preservation axis is dead: RUN 4 preserved 8/10 bits, RUN 7 preserved all of them plus set bit 5 — identical wedge both times. The pcieclkgen mask-RMW clobber was not the confounding variable across RUNs S..6.
- **Consolidated interpretation across A..7:** phy_ip is bidirectionally decode-locked from every fabric state we've built, now including the strongest (RUN 7: naked axi2af + bit-5-only pcieclkgen with iBoot bits intact + CLK0/1 ACKed + RESET clear + T8140 marker + CLK_MODE=ON + phy_shared+0 bit 9 SET). Remaining live hypotheses, ranked: (i) **candidate C `set32(phy_shared+4, 0x10)`** (RUN 8 = primary); (ii) **candidate D T602X-style `set32(phy_shared+0, 0x300)`** (bits 8+9 — bit 8 has never been set; RUN 9 fallback); (iii) C-side vs Python sequencing gap (barriers / back-to-back timing vs multi-ms proxy round-trips); (iv) PMGR/power-domain (some phy/auspma/cio PS register down at the wedge point — data collection starts in RUN 8).

**Proposed RUN 8 plan (2026-07-21):**

**RUN 8 = candidate C: `--phy4-x10-early` on the RUN 7 baseline.** The T602X/T8122-only `set32(phy_base+4, 0x10)` (pcie.c:529) that the T8140 codepath skips. RUN L tested it early-but-ALONE and wedged; it has never run combined with the current strongest baseline. Dispatcher-only for the hypothesis — the flag and its 6.i.early apply block already exist in perstn.py (from RUN L), firing after 6.f and before 7.early/6.5/6.g.

Key ordering observation: with the RUN 8 flag set, the produced write order is `phy_shared+4 |= 0x10` (6.i) → phycmn MODE_ON (7.early) → `phy_shared+0 |= 0x200` (6.5) — which reproduces T8122's native pcie.c order 529 → 535 → 543-551, merely hoisted above the phy_ip tunables. RUN 8 therefore replays the entire T8122 tail in T8122 order before the first phy_ip touch.

Expected transition: `phy_shared+4: 0x00000001` (post-6.f) → `0x00000011`. RUN L observed the same bits stick without faulting, so the write itself is proven STUCK-capable.

Single-variable delta vs RUN 7: only `--phy4-x10-early` is added as a state-changing write. `--t8122-shared-post` and `--pcieclkgen-set5-only-to` are retained even though falsified as ungates (dropping either would be a second variable change). Additionally RUN 8 adds `--pmgr-pre6g-scan` — a strictly READ-ONLY PS-register sweep of every PMGR device named like APCIE/PCIE/PHY/AUSPMA/CIO immediately before step 6.g, diffed against the Phase 0 boot-time readout. Zero writes, flag-gated, flushed via `phaseF.pre.6.g.pmgr-scan` so it survives the wedge; feeds hypothesis (iv) if candidates C and D both fail.

RUN 8 dispatch flags:
```
--no-pcie-init --preinit-probe --pmgr-enable --pmgr-per-port --gate-poke
--phy-ip-probe --t8140-replay --phy-ip-diag --fuse-recon
--naked-write-test-readonly --axi2af-naked-apply
--pcieclkgen-set5-only-to=axi_sub5_base --reachable-scan --phycmn-early
--phy4-x10-early --t8122-shared-post --phy-ip-diag-at=none
--pmgr-pre6g-scan
```

RUN 8 wedge-immune posture: `--phy-ip-diag-at=none` retained. The existing `diag("post-6.i.phy4-x10-early")` checkpoint captures a full reachable-scan right after the new write at zero extra risk — watch `phy_shared+0x04` (expect `0x00000011`) and `phy_shared+0x08` (the T8122 poll target that has always read 0; a nonzero read there would be high-signal even if 6.g still wedges).

Interpretation matrix for RUN 8:
- **6.g STOPS wedging** → candidate C confirmed: bit 4 of `phy_shared+4` is the decode gate the T8140 codepath misses on t8132. Phase F continues automatically (6.h auspma tunables, steps 8/9/10 RC handshake, success banner). Follow-ups: next boot runs `p.pcie_init()` end-to-end; draft the m1n1 patch (add `phy_base+4 |= 0x10` before the phy_ip tunables in the t8132 path, plus the port-1 slice filter).
- **6.g wedges identically** at `phy_ip+0x38` → candidate C falsified. RUN 9 = candidate D: T602X-style `set32(phy_shared+0, 0x300)` (pcie.c:549). Implementation: parametrize the 6.5 block value — new `--t8122-shared-post-val=0x300` (default `0x200`) threading through the existing block. Single-variable delta vs RUN 8.
- **6.g wedges at a NEW address / new fault class** (e.g. SYNC instead of UartTimeout) → partial ungate; highest-signal outcome short of success. Analyze the new address against the tunables entry list before committing to RUN 9.
- **SError/guard-delta inside 6.i itself** → bit 4 interacts with the combined state in a way RUN L's isolated test didn't show. Analyze before proceeding.
- **If C and D both fail** → the Python-replayable pcie.c write set is exhausted. Follow-on axes, in order: (a) **sequencing gap** — port the port-1 slice filter into the m1n1 fork's `pcie_init_controller()` and let the C side run 6.g natively at CPU speed with barriers (one rebuild/reflash), or half-step: upload a tiny stub via the proxy and `p.call()` it so the tunable writes execute back-to-back on-CPU; (b) **PMGR angle** — driven by the RUN 8/9 pre-6.g sweep data: `pmgr_adt_power_enable` any matched device not at ACTUAL=0xf before 6.g.

**State as of 2026-07-21 (post RUN 8):**
- **RUN 8 hypothesis FALSIFIED.** `--phy4-x10-early` executed perfectly: `phy_shared+4` went `0x00000001` → `0x00000011` (bit 4 STUCK, guard delta=0, `Scripts/m1n1/logs/8/nic-runtime.txt:3975-3977`, post-scan line 4204). `phy_shared+0x8` stayed `0x00000000` — the T8122 poll target is not activated by bit 4 either. Step 6.g wedged IDENTICALLY: `RAISED at #0 (shared) 0x497040038: UartTimeout` (line 5773). Bit 4 of `phy_shared+4` is not the phy_ip decode gate, even with the full T8122 tail replayed in native order (529 → 535 → 543-551) before the phy_ip tunables. Full findings in `Scripts/m1n1/logs/8/findings.md`.
- **First PMGR sweep dataset (new `--pmgr-pre6g-scan`, lines 5724-5765, 39 matched devices): every APCIE-family gate is ON (`actual=0xf`) at the wedge point** — APCIE_GP, APCIE_SYS_GP, APCIE_ST, APCIE_SYS_ST, APCIE_PHY_SW. The single DELTA vs Phase 0 (gate 151 APCIE_PHY_SW `0x4 → 0xf`) is the expected Phase D poke. All OFF devices are unrelated ATC*/DPTX/CIO Type-C tunnels; no `dev_disable`/`parent_off`/`RESET` flags anywhere in the apcie/phy family. **The PMGR/power-domain hypothesis is WEAKENED** — nothing observable in PS registers blocks phy_ip.
- **Consolidated interpretation across A..8:** phy_ip is bidirectionally decode-locked from every state we can build, and the T8122/T602X pre-phy_ip write inventory in pcie.c is nearly exhausted — the ONLY un-replayed variant left is T602X's `set32(phy_shared+0, 0x300)` (bit 8 has never been set on t8132). Remaining live hypotheses, ranked: (i) **candidate D `0x300`** (RUN 9 = primary); (ii) **C-side vs Python sequencing gap** (RUN 10 axis: `p.call()` stub executing the 29 pll-tunable writes back-to-back on-CPU, then C-side native 6.g with the port-1 slice filter); (iii) PMGR angle — weakened, only viable via non-PS gating.

**Proposed RUN 9 plan (2026-07-21):**

**RUN 9 = candidate D: `--t8122-shared-post-val=0x300` on the RUN 8 baseline.** T602X's shared-init post-write variant (pcie.c:549, bits 8+9) vs the T8122 `0x200` (bit 9 only, pcie.c:551) that RUNs 6/7/8 applied. Implementation: the 6.5 block's write value is parametrized via a new `--t8122-shared-post-val` flag (default `0x200`, preserving RUN 6-8 behavior); step label, masked pre/post reporting, and STUCK classification generalize accordingly.

Expected transition: `phy_shared+0: 0xf3c0301f → 0xf3c0331f` (bits 8+9 SET in one write, delta `0x300`).

Single-variable delta vs RUN 8: only the 6.5 write value changes `0x200 → 0x300`. All falsified-but-retained flags kept (`--phy4-x10-early`, `--pcieclkgen-set5-only-to`, `--t8122-shared-post`) — dropping any would be a second variable change. `--pmgr-pre6g-scan` retained (read-only, free differential data each boot).

RUN 9 dispatch flags:
```
--no-pcie-init --preinit-probe --pmgr-enable --pmgr-per-port --gate-poke
--phy-ip-probe --t8140-replay --phy-ip-diag --fuse-recon
--naked-write-test-readonly --axi2af-naked-apply
--pcieclkgen-set5-only-to=axi_sub5_base --reachable-scan --phycmn-early
--phy4-x10-early --t8122-shared-post --t8122-shared-post-val=0x300
--phy-ip-diag-at=none --pmgr-pre6g-scan
```

Interpretation matrix for RUN 9:
- **6.g STOPS wedging** → bit 8 (alone or with bit 9) is the phy_ip decode gate. Phase F continues automatically (6.h, RC handshake 8/9/10, success banner). Follow-ups: next boot `p.pcie_init()` end-to-end; m1n1 patch candidate (T602X-style post-write in the t8132 path + port-1 slice filter).
- **6.g wedges identically** at `phy_ip+0x38` → candidate D falsified and **the Python-replayable pcie.c write set is EXHAUSTED**. RUN 10 = sequencing-gap axis: (a) cheapest half-step, no reflash — upload a tiny AArch64 stub via the proxy and `p.call()` it so the 29 apcie-phy-ip-pll-tunables writes execute back-to-back on-CPU with barriers (isolates the multi-ms USB round-trip variable); (b) full step — port the port-1 slice filter into the m1n1 fork's `pcie_init_controller()` (pcie.c:518-525 region) and let the C side run 6.g natively (one rebuild/reflash).
- **6.g wedges at a NEW address / new fault class** → partial ungate; highest-signal outcome short of success. Analyze before RUN 10.
- **SError inside 6.5 with 0x300** → bit 8 write faults where bit 9 didn't; high-signal — bit 8 touches something live.

**Related memories:** [[ref-m4-repos]] (repo paths + tooling), [[ref-asahi-t8132-pcie]] (upstream Linux + Asahi source-of-truth reference)
