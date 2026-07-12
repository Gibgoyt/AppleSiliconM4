I have enough data. Let me synthesize the findings and propose a plan.

## RUN S findings

### 1. Step 5.8.a (axi2af naked apply → axi_base) — mostly landed
```
STUCK     : 14  (bit-31 sets @ axi_base+0x00..0x34 except +0x38, and +0x3c)
NO-OP     :  2  (@ axi_base+0x38 and +0x40 — both bit-31 sets)
SKIP-NOOP : 42  (pre-value already matched target — no write needed)
PARTIAL   :  0
READ_FAIL :  0
total     : 58 of 58
```
- 14 registers accepted bit-31 sets. Confirms the naked-write path is broadly viable for axi_base.
- **Two entries specifically refused bit-31**: `+0x38` (val=0x80000016) and `+0x40` (val=0x8000000c). Same mask (0x800000ff) as the STUCK ones. Adjacent entry `+0x3c` (identical mask, identical value 0x8000000c to #16) DID stick — so it's not the value, it's those specific offsets.
- SKIP-NOOP count (42) means most axi_base tunables were already at target values from ADT-side pre-programming — nothing to do.

### 2. Step 5.8.b (pcieclkgen naked apply → axi_sub5_base) — landed cleanly
```
#0 @ 0x495046200 mask=0x3e0 value=0x220 pre=0x00000a01 new=0x00000a21 post=0x00000a21  [STUCK]
STUCK: 1, total: 1 of 1
```
Bits 5-9 landed at axi_sub5_base+0 as intended.

### 3. Step 6.g — **STILL STALLS on the same offset**
```
RAISED at #0 (shared) 0x497040038: UartTimeout: Expected 1 bytes, got 0 bytes
```
Same wedge as every RUN A..R. phy_ip+0x38 is unreachable.

## Signal that jumps out

phy_ip stalls at **offset 0x38**. Of the 58 axi2af entries applied at axi_base, the ONLY two that refused bit-31 sets were **axi_base+0x38 and axi_base+0x40**. The offset alignment between "axi_base register we can't set bit 31 on" and "phy_ip register that AXI-stalls" is 0x38. This is either a genuine aperture/enable-bit correlation or a coincidence.

Findings 1/7 (the "bit-31 unlock at axi_base" and "pcieclkgen bits at axi_sub5_base+0") are **necessary but not sufficient** for phy_ip ungate. Something else is still gating.

## What's still unknown

- **Why do axi_base+0x38 and +0x40 reject bit 31?** Are they read-only in the current fabric state? Is there an upstream unlock write that would allow them? Were they SKIP-NOOP earlier and became NO-OP later because some intermediate write cleared them?
- **Does the axi_base+0x00 bit-31 survive to step 6.g?** RUN R set 0x8000001c naked. RUN S's entry #0 was SKIP-NOOP with pre=0x8000001c. But the tail of the RUN S log shows the axi_base reachable-scan post-6.f reads back `+0x00=0x0000001c` (bit 31 gone). Something between 5.8.a and 6.f cleared bit 31 at axi_base+0 — likely one of steps 6.a-6.f writing to phy_shared has a side-effect on axi_base. Worth confirming via mid-step scans.
- **Is the pcieclkgen apply target correct?** RUN S applied to axi_sub5_base per RUN R's first-touch data. But per the pcieclkgen ADT declaration, m1n1 would apply to reg_idx=1 (rc_base). Do we need BOTH applies, or is axi_sub5_base wrong?
- **The phy_common CLK_MODE=ON ordering** (RUN I) has never been tried in combination with the RUN S naked applies.

## Proposed RUN T ladder

Rather than jump straight to baking Findings 1/7 into m1n1, propose four single-variable extensions of RUN S:

| RUN | Change vs S | Hypothesis being tested |
|---|---|---|
| **T** | Add `--phycmn-early` (RUN I's mask32 phy_common+0 MODE=ON) between 5.8.b and 6.g | CLK_MODE=ON in combination with the naked applies unlocks phy_ip |
| **U** | Add mid-step reachable-scans at post-6.a, post-6.b, post-6.c, post-6.d, post-6.e, post-6.f (specifically checking axi_base+0..0x40) | Determine which of steps 6.a-6.f clears axi_base+0 bit 31 (side-effect diagnosis) |
| **V** | Rerun 5.8.a NO-OP entries with mask-widened writes (try mask=0xffffffff to axi_base+0x38 and +0x40 to probe R/O vs upstream-gated) | Distinguish R/O from write-locked-until-unlock |
| **W** | Apply pcieclkgen ALSO to rc_base+0 (its ADT-declared reg_idx=1 target), keeping the axi_sub5_base apply too | Test whether pcieclkgen needs BOTH targets (RUN R + RUN S interpretation) |


