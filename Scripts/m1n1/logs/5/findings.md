# RUN 5 findings

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 5` (dispatcher `c9d5991`, logs `ee625c3`).

**Hypothesis (from `docs/project-m4-pcie-bringup.md:176-186`):** phy_ip decodes
POSTED writes at `+0x38` even when reads AXI-stall, from the cleanest fabric
state ever probed for write-decode (naked axi2af + naked pcieclkgen landed at
sub5, CLK_MODE=ON, iBoot mostly preserved).

**Result: FALSIFIED (STALL branch).** The posted `write32(phy_ip_base + 0x38, 0)`
at `post-7.phycmn-early` bus-hung the CPU on m1n1's side just like every prior
read wedge. phy_ip is bidirectionally decode-locked from the RUN 4 baseline
state. The entire write-only strategy branch (candidate A's WROTE outcome +
follow-on 29-entry naked-write32 replay of `apcie-phy-ip-pll-tunables`) is now
eliminated.

## The wedge

`Scripts/m1n1/logs/5/nic-runtime.txt:4422` (end of file, mid-line):

    [phy-ip-write-probe @ post-7.phycmn-early] write32(0x497040038, 0) [phy_ip + 0x38]:

Colon has no result after it. No `WROTE`, no `exc_count delta`, no post-write
readback, no traceback. `Scripts/m1n1/logs/5/run.log` confirms the final flush
was `phaseF.diag.post-7.phycmn-early.phy-ip-write-enter` at 336398 bytes (=
exact file size on disk). The write initiated, the "enter" marker was emitted,
then m1n1's UART froze before the post-write flush could run.

Failure signature: identical to every RUN A..4 read wedge at 6.g. Same address
(`phy_ip_base + 0x38 = 0x497040038`), same error class (m1n1 UART
non-responsive), same recovery path (host abort).

## Phase F progression: further than any prior RUN

For the first time we cleanly walked all of steps 6.a-6.f and step 7 (via
`--phycmn-early`) before wedging:

| Step | Write | guard delta | Log line |
|---|---|---|---|
| 5.75.naked-write-test READ-ONLY | pre-reads only (RUN 4 mode) | 0 | 1555-2041 |
| 5.8.a.axi2af-naked-apply | 15 STUCK / 2 NO-OP / 41 SKIP-NOOP | 0 | 2043-2205 |
| 5.8.b.pcieclkgen-naked-apply | sub5+0 `0x00081f55` → `0x00081e35` STUCK | 0 | 2641-2658 |
| 6.a.set32(phy_shared+0, CLK0REQ) | landed | 0 | 3480-3490 |
| 6.b.poll_CLK0ACK | converged (bit 2 set) | 0 | 3490-3500 |
| 6.c.set32(phy_shared+0, CLK1REQ) | landed | 0 | 3800-3810 |
| 6.d.poll_CLK1ACK | converged (bit 3 set) | 0 | 3810-3820 |
| 6.e.clear32(phy_shared+0, RESET) | bit 7 cleared | 0 | 3960-3975 |
| 6.f.set32(phy_shared+4, 0x01) | T8140 marker landed | 0 | 3975-3985 |
| 7.mask32(phy_common+0, MODE_ON) | phy_common+0 → `0x80300001` | 0 | 3986-3990 |
| post-7.phycmn-early diag + full reachable-scan | rc/phy_common/phy_shared/axi/sub5/sub6 all read cleanly | 0 (all offsets) | 3992-4421 |
| **post-7.phycmn-early phy-ip-write-probe** | **WEDGED at first write32** | (UART froze) | **4422** |

Every guard delta before 4422 = 0. No SError anywhere. All prior RUN A..4
wedges happened at step 6.g on a phy_ip READ; RUN 5 is the first to exercise
the post-step-7 fabric state and confirm phy_ip is still locked.

## Register snapshot at post-7.phycmn-early

Read-clean at line 3989 onward. Key values (all reachable, delta=0):

| Base + offset | Value | Interpretation |
|---|---|---|
| `phy_common+0`  | `0x80300001` | bit 31 (100MHz) + bits 20-21 (guesswork per pcie.c:50) + bit 0 (CLK_MODE_ON) ✓ landed |
| `phy_common+2a00..2c00` | all zero | atc.c CIO3PLL analogy still confirmed as non-applicable (per RUN 3) |
| `phy_shared+0`  | `0xf3c0301f` | bits 0-4 set (CLK0/1 REQ+ACK + ???), bits 12-13, 20-21, 26-29, 30-31 set; **bits 8, 9 CLEAR** |
| `phy_shared+4`  | `0x00000001` | T8140 marker ✓ landed |
| `phy_shared+8`  | `0x00000000` | **bit 0 CLEAR** — see next section |
| `axi_base+0`    | `0x0000001c` | bit 31 CLEAR ("clear window" active per RUN 4 finding) |
| `axi_base+0x38` | `0x00000016` | bit 31 CLEAR (matches RUNs S/1/4 axi2af NO-OP #14) |
| `axi_base+0x40` | `0x0000000c` | bit 31 CLEAR (matches RUNs S/1/4 axi2af NO-OP #16) |
| `axi_sub5+0`    | `0x00081e35` | pcieclkgen mask-RMW landed, iBoot bits 2/4/10/11/12/19 preserved |
| `axi_sub5+0x100`| `0x030b40b4` | cio3pllcore #6 target value already present (iBoot pre-programmed, per RUN 2) |
| `axi_sub6+0`    | `0x00000239` | matches RUN 4 iBoot value |

Cross-checkpoint sub5+0 persistence:

| Checkpoint | sub5+0 | Delta |
|---|---|---|
| post-5.75 (READ-ONLY) | `0x00081f55` (iBoot untouched) | — |
| post-5.8.b (pcieclkgen STUCK) | `0x00081e35` | mask-0x3e0 bits 5-9 mutated (bit 5+9 set, bits 6+8 cleared) |
| post-6.b.CLK0ACK | `0x00081e35` | (stable) |
| post-6.d.CLK1ACK | `0x00081e35` | (stable) |
| **post-7.phycmn-early** | **`0x00081e35`** | (stable across all 7 checkpoints) |

Nothing between pcieclkgen apply and step 7 mutates sub5+0. Consistent with
RUN 4.

## THE key new finding: `phy_shared+0x8 = 0x00000000`

pcie.c:543-551 (`m1n1/src/pcie.c`):

    if (state->pcie_regs->type == APCIE_T602X ||
        state->pcie_regs->type == APCIE_T8122 ||
        state->pcie_regs->type == APCIE_T6031) {
        // Why always PHY 1 in this case?
        u32 off = state->num_phys > 1 ? PHY_STRIDE : 0;
        if (poll32(state->phy_base[0] + off + 0x8, 1, 1, 250000)) {
            printf("pcie: PHY clock enable timed out\n");
            return -1;
        }
        for (int phy = 0; phy < state->num_phys; phy++) {
            if (state->pcie_regs->type == APCIE_T602X) {
                set32(state->phy_base[phy] + APCIE_PHY_CTRL, 0x300);
            } else if (state->pcie_regs->compat == APCIE_T8122) {
                set32(state->phy_base[phy] + APCIE_PHY_CTRL, 0x200);
            }
        }
    }

T8140 skips this block entirely. T8122/T602X/T6031 poll `phy_shared+0x8` bit 0
= 1 with a 250ms timeout, then `set32(phy_shared+0, 0x200)` (T8122) or
`0x300` (T602X). We are the first to observe this offset's state on t8132.

**`phy_shared+0x8 = 0x00000000` at post-7.phycmn-early** — bit 0 is NOT set.
Two implications:

1. The C-side poll (if we ran it verbatim) would **time out at 250 ms** and
   return `-1`. This is presumably why the RE loop's T8140 path skips it —
   T8140 chips don't have this handshake, and running the poll on them would
   just waste 250ms and fail.

2. But t8132 is APCIE-generation closer to T8122 than T8140. If it needs the
   `set32(phy_shared+0, 0x200)` post-write (bit 9 = phy_ip decode enable, or
   similar gating bit), and we're not doing it, phy_ip stays decode-locked.
   Which matches exactly what we see.

**Hypothesis for RUN 6:** bit 9 of `phy_shared+0` (value `0x200`) is a phy_ip
decode-enable gate that T8140 doesn't have but T8122/T8132 do. Setting
`phy_shared+0 |= 0x200` after step 7 (phycmn-early) unlocks phy_ip decode.
The C-side poll on `phy_shared+8` is a status handshake for a wait we don't
strictly need (may just be sequencing/settling) — do the `set32` unconditionally
and skip the poll to avoid the 250 ms timeout.

## Ruled OUT by RUN 5

- **Write-only strategy branch:** if phy_ip decodes writes but not reads, we
  could sequence the 29-entry `apcie-phy-ip-pll-tunables` shared slice via
  naked `write32`. RUN 5 STALL proves phy_ip is BIDIRECTIONALLY decode-locked
  — writes AXI-stall the same as reads. No write-only strategy is viable
  from the current baseline.

- **"phy_ip is on-fabric but internal SError on read":** RUN J saw
  `Exception: SYNC` on a phy_ip read with `--extra-tunables`. RUN 5 with the
  much stronger naked-apply baseline shows plain UartTimeout (no exception),
  which means phy_ip is not on-fabric AT ALL from the current state — the
  fabric never routes the transaction to a slave that can respond, so no
  SError, just an infinite AXI stall.

## NOT ruled out (still live for RUN 6+)

- **pcieclkgen bit 6/8 clobber (candidate B from RUN 5 plan):** the pcieclkgen
  mask-RMW clears iBoot bits 6, 8 while setting bits 5, 9. If bits 6, 8 are
  PLL enables and the loss of them keeps the PLL from locking, phy_ip stays
  gated. A bit-5-only `set32` variant would preserve iBoot bits 6, 8 and test
  this axis.
- **T8122 shared-init post writes (candidate C):** promoted to RUN 6 primary
  candidate based on `phy_shared+8 = 0` finding + bit-9 hypothesis above.
- **C-side barrier/DSB/ISB ordering:** RUN P's earlier observation that the
  C-side path reaches LTSSM BUSY (per commit `6b277bc`) while Python replay
  wedges could still be ordering. Deferred until Python-side hypotheses
  exhaust.

## RUN 6 plan

Full plan in `docs/project-m4-pcie-bringup.md` (post-RUN-5 state block).

**RUN 6 = candidate A:** add `--t8122-shared-post` which injects between
step 7 (phycmn-early's `mask32(phy_common+0, ..., CLK_MODE_ON)`) and step
6.g (first phy_ip access). Runs `set32(phy_shared+0, 0x200)` unconditionally,
skips the C-side `poll(phy_shared+0x8, 1, 1, 250000)` because RUN 5 proved
bit 0 is clear (poll would timeout). Diagnostic pre-read of `phy_shared+0x8`
still logs its value for cross-checkpoint comparison.

Single-variable delta vs RUN 4: `--t8122-shared-post` added. Everything else
identical: `--naked-write-test-readonly`, `--axi2af-naked-apply`,
`--pcieclkgen-naked-apply-to=axi_sub5_base`, `--reachable-scan`,
`--phycmn-early`, `--phy-ip-diag-at=none` (wedge-immune).

Success criterion: **step 6.g stops wedging** at `phy_ip+0x38`. If it does,
phy_ip decode is unlocked by `phy_shared+0 |= 0x200` and we complete Phase F
for the first time.

Failure branches for RUN 7:
- Wedges at 6.g identically → bit 9 hypothesis wrong. RUN 7 = candidate B
  (`--pcieclkgen-set5-only`), ~15 lines.
- Wedges somewhere new → high-signal, examine new wedge address.
- SError somewhere in the `--t8122-shared-post` block → the guarded
  Python isolates which specific write causes the SError (a data point that
  the C-side `dc25f2f` commit could not extract because it aborts on first
  SError).
- Something progresses past 6.g but wedges at 8/9/10 → we've cleared the phy_ip
  hurdle and are now debugging RC init. New chapter.
