# RUN 9 findings

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 9` (dispatcher `dbe3c98`).

**Hypothesis (candidate D, from `docs/project-m4-pcie-bringup.md` post-RUN-8
block):** bit 8 of `phy_shared+0` (alone or together with bit 9) is the
phy_ip decode gate. T602X runs `set32(phy_shared+0, 0x300)` (pcie.c:549)
where T8122 runs `0x200` (bit 9 only, pcie.c:551); RUNs 6/7/8 only ever
applied the T8122 variant, so bit 8 had never been set on t8132. RUN 9
switched the 6.5 write to `--t8122-shared-post-val=0x300`.

**Result: FALSIFIED.** Both bits landed cleanly, but step 6.g wedged
IDENTICALLY to every RUN A..8. Same address
`phy_ip_base + 0x38 = 0x497040038`, same entry index #0, same error class
`UartTimeout: Expected 1 bytes, got 0 bytes`.

## The 6.5 block with val=0x300: everything worked

`Scripts/m1n1/logs/9/nic-runtime.txt:5281-5290`:

    --- 6.5.T8122-shared-post replay (pcie.c:543-551, T8140 codepath skips; val=0x300) ---
      pre-read: phy_shared+0x8 = 0x00000000
      bit 0 CLEAR -- C-side poll would timeout at 250 ms; SKIPPING poll
      pre-write: phy_shared+0x0 = 0xf3c0301f (bits(0x300) = 0x0)
    --- 6.5.set32(phy_shared+0, 0x300) [T8122/T602X post-write] ---
    [guard] 6.5.set32(phy_shared+0, 0x300) [T8122/T602X post-write]: exc_count delta = 0 (before=0, after=0)
      post-write: phy_shared+0x0 = 0xf3c0331f (delta=0x00000300, bits(0x300) = 0x300)
      post-write: phy_shared+0x8 = 0x00000000 (delta=0x00000000)
      RESULT: bits 0x300 STUCK -- phy_shared+0 now has the shared-init post-write applied.

- Bits 8+9 BOTH STUCK in a single write; delta exactly `0x300`
- `phy_shared+0x8` unchanged (0 → 0) — bit 8 does not trigger the T8122
  poll-target either
- Post-6.5 scan (line 5517): `+0x00=0xf3c0331f +0x04=0x00000011
  +0x08=0x00000000 +0x0c=0x00000000`

## Everything else identical to RUN 8

- 6.i: `phy_shared+4 = 0x00000011` (marker + bit 4)
- 5.8.b: `axi_sub5+0 = 0x00081f75`, iBoot bits preserved
- PMGR pre-6.g sweep: identical to RUN 8 — 39 matched devices, all
  APCIE-family gates ON (`actual=0xf`), gate 151 delta `0x4 → 0xf` from
  the Phase D poke
- No SError, guard delta=0 everywhere
- **`rc_base+0x4c` note:** RUN 8 vs RUN 9 values differ (`0x0002199d` vs
  `0x000316ef`), but within RUN 9 the register increments monotonically
  at every scan checkpoint (`0x2ae61 → 0x2b496 → 0x2bae9 → 0x2c170 →
  0x2cef6…`). It is a free-running counter/timer — the cross-run delta is
  noise, not a bit-8 side effect.

## The wedge (identical to every prior RUN A..8)

`nic-runtime.txt:5770-5773` (end of log):

    --- 6.g.tunables apcie-phy-ip-pll-tunables (slice-filtered) ---
        plan (29 entries):
          shared               = 29
        RAISED at #0 (shared) 0x497040038: UartTimeout: Expected 1 bytes, got 0 bytes

## What RUN 9 tells us

### Falsified
- **Bits 8 and 9 of `phy_shared+0` are NOT the phy_ip decode gate**, in
  any combination (RUN 6/8: bit 9 alone; RUN 9: bits 8+9).
- **Candidates A, B, C, and D are now all dead.** The Python-replayable
  pre-phy_ip write inventory in pcie.c (T8140, T8122, and T602X branches)
  is EXHAUSTED. Every documented write that any Apple chip family issues
  before the phy_ip tunables has been applied on t8132, verified STUCK,
  and the wedge is unchanged.

### Confirmed
- The strongest state yet: naked axi2af + bit-5-only pcieclkgen (iBoot
  bits intact) + CLK0/1 ACKed + RESET clear + marker `0x11` + CLK_MODE=ON
  + `phy_shared+0 = 0xf3c0331f` (bits 8+9) + all APCIE PMGR gates ON.
  phy_ip still does not decode.

### Not yet ruled out
Ranked:
1. **Tight-timing window — RUN 10 target.** Proxy `read32`/`mask32` are
   executed by m1n1's CPU, so "proxy vs on-CPU" is identical at the
   instruction level; the ONLY remaining sequencing variable is
   inter-access timing. pcie.c reaches the first phy_ip access
   microseconds after the CLK/RESET handshake; our replay takes seconds.
   If decode has a post-handshake window, every RUN A..9 blew through it.
   Test: on-CPU stub replaying the idempotent tail + all 29 RMWs
   back-to-back with barriers.
2. **Aperture ownership / companion-firmware angle.** phy_ip may only
   decode for a different requester (SMC or another coprocessor may own
   PHY init on t8132), or require non-PS gating invisible to PMGR PS
   registers (fabric routing tables, security attributes).
3. **C-side native replay** — low expectation (same instructions, same
   CPU as the stub); only worth it as final closure of the axis if the
   stub also wedges.

## RUN 10 plan

Full plan in `docs/project-m4-pcie-bringup.md` (post-RUN-9 state block).

**RUN 10 = `--phy-ip-stub-apply=tail` on the RUN 9 baseline:** upload an
AArch64 stub (ARMAsm + `p.call`, `upload_and_call.py` pattern) that
re-runs the idempotent shared-init tail (CLK0REQ/ACK, CLK1REQ/ACK, RESET
clear, `+4|=0x11`, MODE_ON, `+0|=0x300` — each followed by `dsb sy`) and
immediately applies the 29 pll tunable mask-RMWs (then the auspma
entries, port-1-inactive slice filtered). Returns `0xC0DE0000|count` on
success, `0xDEAD000x` on tail poll timeout / bad entry size; a wedge
surfaces as UartTimeout on the fully-flushed `p.call` step.

Outcome branches:
- `0xC0DE001d` + live verify reads → BREAKTHROUGH: timing window
  confirmed; Phase F continues.
- UartTimeout → tight-timing axis falsified; phy_ip does not decode for
  the AP from ANY pcie.c-constructible state. RUN 11+ = SMC/companion
  ownership hunt + aperture/security recon.
- `0xDEAD0001/2` → a tail step is not idempotent — high signal about the
  handshake.
- SError/guard-delta → first-ever fault syndrome from phy_ip; analyze.
