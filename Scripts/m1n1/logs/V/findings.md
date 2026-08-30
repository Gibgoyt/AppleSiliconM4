# RUN T/U/V findings (2026-08-30) — phy_ip unlocked, port 2 link UP, NIC enumerated

Driver: `Scripts/m1n1/trace_replay.{py,sh}` + `trace_ops.py`, replaying
Yureka's macOS MMIO trace (`pcie.log`, j773g). Analysis in
`docs/pcie-trace-analysis.md`. Enrolled m1n1: v1.6.0-40-g2ea75b6
(checkout `~/Projects/C/embedded/m1n1`, main @ 2ea75b6 + local changes).

## RUN T — preamble (trace lines 1–34) + phy_ip probe

**PROBE PASS.** `read32(0x497040038) = 0x5c800800` — identical to the
trace value. The register that AXI-stalled every RUN A..S and 1..33 is
reachable after replaying the 20-step preamble. All 16 RMW pre-values
matched the trace exactly; both CLK ack polls converged; 0 faults.

The unlock deltas vs our old RUN S replay (all from the trace, all
previously untried):
1. `axi_base+0x104 |= 1`, `+0x108 |= 1` (first ops)
2. `rc_base+0x04 = 0`
3. per-port PHY pre-writes for all 3 ports BEFORE the CLKREQ handshake
   (`+0x00` clear bit28, `+0x10` set bit16/clear bits10-11, `+0x14`
   set bit26/clear bits20-21)
4. after CLK1ACK: **clear bit 4, KEEP bit 7** at phy_shared+0
   (m1n1-t8140's clear-bit-7 "RESET" is wrong for t8132)
5. none of the `|=0x11` / phy_common MODE / `|=0x300` extras before
   phy_ip

Note: m1n1/USB died shortly after RUN T's clean exit (power-cycle
needed). Cause unknown — possibly an interrupt from the now-powered
fabric with no handler. Happens after each replay session; plan on a
power-cycle per run.

## RUN U — + phyip (35–390) + port2 (817–1253) + ECAM

- phy_ip PLL block: 183 steps, 0 faults, all polls converged.
- PERSTN deassert (gpio0[165]) immediately before the port2 segment.
- Port2: LTSSM kick (`ltssm+0x10=2, +0x1c=4, +0x20|=2, +0x14=1`),
  `port+0x800` clear bit 8, then **link poll CONVERGED:
  `0x492028208 = 0xab000200`** (trace-identical). LINKSTS watch:
  `port2 = 0x9b000001 [UP]`. FIRST LINK-UP EVER on this port outside
  macOS.
- ECAM walk saw the root port `00:02.0 = 106b:100c (PCI bridge)` but
  no endpoint — bridge secondary bus was still 0.
- Single benign mismatch: `L1114 0x492028804` read 0xd vs trace 0x5
  (extra bit 3; status divergence, no effect).

## RUN V — + bridge bus-number programming (macOS trace L1396)

Driver now writes `cfg+0x18 = 0x010100` (pri=0/sec=1/sub=1) on the
port-2 bridge and re-walks:

    01:00.0: VID:DID = 14e4:1682  class=02.00.00 (ethernet)
    CMD 0x0000 -> 0x0006 (readback 0x0006)
    BAR0 raw = 0x0000000c  (64-bit prefetchable, address unassigned)

**The NIC is a Broadcom BCM57762 (14e4:1682)** — Phase 3 deliverable
Q2 (VID:DID) complete. Whole chain reproduced from cold boot twice
(RUN U and RUN V), 594 steps, 0 faults, 0 pre-mismatches both times.

## Next steps (Phase 4 territory)

1. BAR assignment: program bridge memory window (cfg 0x20/0x24) and
   NIC BAR0/BAR1 inside it (macOS's enum segment lines 1254–2163 in
   the trace shows its assignments), set bridge CMD MEM+BM.
2. read32 of BAR0 MMIO → BCM57762 register space (tg3 layout);
   verify chip ID register.
3. dart-apcie2 setup for DMA, then rings/descriptors (tg3) → TCP
   hello world.
4. Longer-term: bake the unlock preamble into m1n1's pcie.c t8132
   clause (replace the t8140 phy sequence with the trace's).
