# macOS MMIO trace analysis (pcie.log from Yureka, 2026-08-30)

> **STATUS 2026-08-30: CONFIRMED BY EXPERIMENT.** RUN T replayed lines
> 1–34 → `phy_ip+0x38` readable (`0x5c800800`, trace-identical). RUN
> U/V replayed phyip+port2 → **port 2 link UP** (`0x492028208 =
> 0xab000200`, LINKSTS `0x9b000001 [UP]`) and the NIC enumerated:
> **`01:00.0 = 14e4:1682` Broadcom BCM57762**. 594 steps, 0 faults, 0
> pre-mismatches, reproduced twice from cold boot. See
> `Scripts/m1n1/logs/V/findings.md`.

Source: 2163-line MMIO capture of macOS initializing PCIe on an M4 Mac
mini (j773g), sent by Yureka with the ask: *"If you figure out which MMIO
writes are relevant for the Link to come up, let me know!"*

Verdict for our project: **this resolves the phy_ip wedge.** Line 35 is
the first access to `phy_ip+0x38` (`0x497040038`) — the exact register
every RUN A..S and 1..33 AXI-stalled on — and macOS reads it
successfully. Therefore lines 1–34 are, by construction, the complete
unlock preamble. Confirmed same hardware state as our chainloaded-m1n1
runs (pre-values match: `PHY_LANE_CFG=0x33000070` in RUN 19/20/22,
handshake acks `0xf3c03095`/`0xf3c0309f` in RUN S, phy_common
`0x80300000`), so the trace transplants directly.

Tooling: `Scripts/m1n1/trace_ops.py` (parser/planner, offline-runnable)
and `Scripts/m1n1/trace_replay.py` + `trace_replay.sh` (RUN T/U driver).

## Region map (trace label -> our name, per pcie_regs.py)

| trace | base | our name |
|---|---|---|
| apcie[0] | 0x1cb0000000 | ECAM |
| apcie[1] | 0x494000000 | rc_base |
| apcie[2] | 0x497000000 | phy_packed (phy_common=+0x4000, phy_shared=+0x8000, port phys +0x20000/+0x24000/+0x28000) |
| apcie[3] | 0x497040000 | phy_ip |
| apcie[4] | 0x496000000 | axi_base |
| apcie[7]/[8] | 0x490028000 / 0x49003c000 | port0 port_base / ltssm |
| apcie[23]/[24] | 0x492028000 / 0x49203c000 | port2 port_base / ltssm |

## Segments (trace line ranges)

| lines | content |
|---|---|
| 1–34 | **unlock preamble** (see below) |
| 35–390 | phy_ip PLL/tunable programming + shared-phy finish (phy_shared+4 \|= 0x10, phy_common MODE=ON, phy_shared+0 \|= bit27, rc+0x54=0x140, rc+0x50=1) |
| 391–816 | port 0 (WiFi) bring-up, LTSSM kick, link-up poll, config |
| 817–1253 | port 2 (**our NIC**) bring-up, LTSSM kick, link-up poll |
| 1254–2163 | ECAM enumeration + endpoint config |

## The unlock preamble (lines 1–34) vs what we were doing

Deltas vs our RUN S replay of m1n1's t8140 path — none of these were
ever tried in runs A..S / 1..33:

1. **First ops in the trace:** `axi_base+0x104 |= 1`, `axi_base+0x108
   |= 1` (0x496000104/0x108, pre 0x10000/0x41330). The ADT axi2af
   tunables at these offsets have masks `0xff0000`/`0xffff0006` — bit 0
   is outside both masks, so tunable application alone can never set it.
   Looks like clock/fabric enables.
2. `rc_base+0x04 = 0` (naked write; we read 0x4 there in RUN S scans).
3. **Per-port PHY pre-writes for ALL 3 ports** (incl. bridge-less
   port 1) *before* the CLKREQ handshake, at 0x497020000/24000/28000:
   `+0x00` clear bit 28 (0x33000070→0x23000070), `+0x10`
   0x300c03→0x310003 (set bit 16, clear bits 10–11), `+0x14`
   0x300c03→0x4000c03 (set bit 26, clear bits 20–21).
4. CLK0REQ/CLK0ACK + CLK1REQ/CLK1ACK handshake — identical to ours.
5. **After CLK1ACK: write `0xf3c0308f` — clears BIT 4, KEEPS bit 7.**
   Our replay did m1n1-t8140's `clear32(phy_shared+0, BIT(7))`
   ("RESET"), producing 0xf3c0301f. On t8132 the bit roles evidently
   moved: bit 7 must stay set, bit 4 is the release. Prime suspect for
   the phy_ip AXI stall.
6. `phy_shared+4 = 1` plain. macOS does **not** do our extras before
   touching phy_ip (no `|= 0x11`, no phy_common MODE=ON, no `|= 0x300`
   — those come *later*, in the lines 379–390 shared-phy finish).

## Answer draft for Yureka ("which writes are relevant for the Link")

- **PHY/aperture unlock:** trace lines 1–34 (everything before the
  first `0x497040038` access). The load-bearing suspects: the two
  `|= 1` writes at `0x496000104`/`0x496000108`, and the
  `0x497008000` sequence ending in `0xf3c0308f` (clear bit 4, keep
  bit 7) + `0x497008004 = 1`.
- **Per port N (base `0x49N028000`, LTSSM `0x49N03c000`):** the port
  register programming block, then the LTSSM kick
  `ltssm+0x10=2, +0x1c=4, +0x20|=2, +0x14=1`, then
  `port+0x800: 0x100101 -> 0x100001` (clear bit 8 = internal PERST
  release). Link status = `port+0x208`: polls at `0x83000204` during
  training, `0xab000200` when the link is up (port 0 identical
  pattern).
- The interleaved reads of `port_phy+0x0` (`0x49702N000`) during the
  poll are just status watches; the refclk handshake on that register
  (bit0/ack bit2, bit1/ack bit3, clear bit4, set 0x200/0x400) happens
  within the port segment.

## How to run the replay (hardware)

    # RUN T: preamble + phy_ip probe (the experiment)
    ./Scripts/m1n1/trace_replay.sh

    # RUN U: continue to link-up + ECAM (only after RUN T passes)
    ./Scripts/m1n1/trace_replay.sh --segments preamble,phyip,port2 --ecam

Replay semantics: R;W pairs become mask-RMWs (only the trace's bit
delta is applied; live pre-value mismatches are logged, not forced),
repeated reads become bounded polls, W-then-changed-R becomes an
ack-mask poll, and every step runs under the exc-guard with partial-log
flushes so a wedge still leaves evidence in
`/tmp/m4-recon/nic-runtime.txt`.
