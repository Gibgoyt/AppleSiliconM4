# RUN 16 findings

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 16` (dispatcher `71ae3b9`) on
m1n1 `8a569ad`. Both RUN 16 fixes worked: the post-init PMGR verification
enabled Phase F (`all real apcie gates ACTIVE`, gate 151 `0x0f0002ff`
actual=0xf post-init), and the run flow was exactly as designed.

**Result: post-init + re-pass + per-entry FALSIFIED.** Phase F ran
1→6.f from the post-`pcie_init` state — the first time ever — and 6.g
wedged at entry #0 (`0x497040038`, UartTimeout) exactly as in every
cold-state run.

## Post-init Phase F step data (new)

- Steps 1/2/3/5 all returned 0 (pmgr, axi2af 58 entries, rc+0x4,
  phy-tunables 10 entries).
- 6.a: `phy_shared+0` was already `0xf3c0301f`-family (CLK req/ack bits
  live from the C init's shared handshake); 6.b/6.d ACK polls converged
  in 0.1 ms; 6.e RESET clear fine.
- 6.f: `phy_shared+4` pre `0x00000001` → post `0x00000001` (idempotent —
  the C init had already set the marker).
- The re-pass was register-visibly a no-op: the post-init state already
  contained everything the re-pass writes. And 6.g (python per-entry
  `mask32`) wedged at entry #0 regardless.

Early tier-1 dump values (same as RUN 15's golden parity):
LINKSTS `0x8300020c`/`0x83000204`, APPCLK `0x00100101`,
T602X_RESET `0x00000001`, PORT_RESET `0x00200001`, STATUS `0x00000005`.

## Corrected 2026-07-11 archaeology

- **Hard contemporary evidence** (commit `c4c0b33`, 2026-07-11 13:23):
  *"Run on 2026-07-11 flushed step 6.g's post-marker at 29649 bytes,
  then wedged before step 6.h could emit its pre-flush."* A Jul-11 boot
  really did complete 6.g — via the **d664bd9-era method:
  `p.tunables_apply_local(path, "apcie-phy-ip-pll-tunables", 3)`, the
  C-side applicator** (the python filter didn't exist yet; c4c0b33
  created it because 6.h's C-applicator hit the port-1 slice).
- **The "pcie_init ran first in that boot" belief is doubtful and was
  partly circular** — it came from comments written weeks later during
  this investigation. Against it: `pcie_up_2` (Jul 10) proved the
  then-enrolled `6b277bc` m1n1 wedges inside `pcie_init`; the
  `--no-pcie-init` flag already existed at `d664bd9`; and Phase F was
  created precisely as the pcie_init replacement. The Jul-11 boot most
  likely ran cold: Phase D poke + Phase F 1→6.f + C-applicator 6.g.
- No actual log file of that boot survives in git (verified) — the
  commit message is the only primary record.

## The combination matrix

| State | per-entry 6.g | C-applicator 6.g |
|---|---|---|
| cold + Phase F | wedge (RUNs A..10 et al) | **UNTESTED — likely Jul-11 winner** |
| post-init + Phase F | wedge (RUN 16) | untested |
| post-init, no re-pass | — | wedge (RUN 14) |

(RUN 10's on-CPU stub was per-entry-equivalent and ran on a polluted
RUN-9 baseline; it does not cover the cold+C-applicator cell.)

## RUN 17 plan

`./Scripts/m1n1/perstn-run.sh 17` — no reflash. Cold-boot flow matching
Jul-11 (`--no-pcie-init --preinit-probe --gate-poke --t8140-replay
--phy-ip-diag --reachable-scan --phy-ip-diag-at=none`) plus
`--phyip-apply-local`: 6.g via the C applicator (29 shared-slice pll
entries, no port risk); 6.h stays python-filtered. Matrix:
- 6.g returns 0 → method confirmed; Phase F continues to
  `PHASE F SUCCESS`; Tier 3a harvest fires (gated); RUN 18 = ports/LTSSM
  from a tuned controller.
- 6.g wedges → cold+C-applicator falsified; the Jul-11 success has no
  reproducible recipe left → pivots: cio3pllcore/pcieclkgen→rc_base
  pre-6.g; m1n1 bisect (Jul-10-era binary vs 8a569ad); or attack
  LTSSM/link-training directly without the phy-ip tunables.
