# RUN 17 findings — the phy-ip chapter closes

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 17` (dispatcher `620d77f`) on
m1n1 `8a569ad` (guard OK). Cold-boot Phase F, native order, 6.g via the
C-side applicator (`p.tunables_apply_local(reg_idx=3)`, the d664bd9-era
method).

**Result: WEDGE at 6.g, entry #0 (`0x497040038`), UartTimeout** — after a
textbook cold-state walk of steps 1→6.f (`phy_shared+0` progression
`0xf3c03090 → 0xf3c03095 → 0xf3c0309f`, identical to every prior cold
run; all diag scans clean). `nic-runtime.txt:2162-2163`:

    --- 6.g.tunables apcie-phy-ip-pll-tunables reg_idx=3 (C-applicator, d664bd9 recipe) ---
      RAISED: UartTimeout: Expected 1 bytes, got 0 bytes

Flush trail: `pre.6.g` 167644 → `post.6.g` 167699 (+55 bytes — the
post-flush marker write; the RAISED text sits in the unflushed buffer).

## The combination matrix — now complete, all cells wedge

| State | per-entry 6.g | C-applicator 6.g |
|---|---|---|
| cold + Phase F | wedge ×30 | **wedge (RUN 17)** |
| post-init + Phase F | wedge (RUN 16) | — |
| post-init, no re-pass | — | wedge (RUN 14) |

## THE REINTERPRETATION — the Jul-11 "success" was a misread wedge

The 2026-07-11 record (c4c0b33: *"flushed step 6.g's post-marker at
29649 bytes, then wedged before step 6.h could emit its pre-flush"*) was
read as "6.g succeeded, 6.h wedged". Code archaeology shows that reading
was never supported:

- The era's `step()` **flushed the post marker on the exception path
  too** (explicitly from `c3c90ed`, same day) — a WEDGED 6.g produces
  post.6.g flush + no 6.h markers + dead m1n1, the *identical*
  observable signature.
- RUN 17 just reproduced that exact trail with a certain wedge.
- No log of the Jul-11 boot survives; the commit message is the only
  record, and it merely proves the step *terminated*, not that it
  succeeded.

**Verdict: phy_ip (`0x497040000`) has NEVER decoded on this machine.**
37 boots across every reconstructable state × method wedge at the first
touch. `6b277bc`'s premise ("phy-ip tunables fix LTSSM stuck at BUSY")
was never validated — its first test (`pcie_up_2`) wedged. The phy-ip
tunables axis is closed. (Whether the window is fused off, owned by
another agent, or needs an unlock nobody has found is academically open
— but no longer the path to a working NIC.)

## The pivot: the evidenced blocker is link training

What actually works: `pcie_init` (tunables-skip m1n1) returns 0, ports
power up, **LTSSM training starts** (LINKSTS BUSY) — and never
converges. Key evidence assembled:

1. Historical kick data (`pcie_up_1.log:145-182`): post-init writes to
   `rc_base+0x3c` and `port+0x10` are **silently dropped** (read back
   0), reset cycles don't move LINKSTS — port config registers look
   write-locked while BUSY. Forcing the skipped port-body steps
   post-hoc is unlikely to work until BUSY resolves.
2. **The CLKREQ# pin is driven against its ADT function**: the ADT
   declares `function_clkreq = GPIO(162, alt-func 2)` — the apcie
   controller manages the CLKREQ#/refclk handshake itself — but every
   run since the pcie_up era forces the pin to a manual GPIO output LOW
   (perstn.py's own comment acknowledged the mux-back was deferred "to
   iteration N+1"... which never came). A broken refclk-request
   handshake is a canonical way to leave LTSSM spinning.
3. The C-side idle poll gives BUSY only 250 ms; a slow endpoint looks
   identical to a dead one.

## RUN 18 plan (no reflash)

`./Scripts/m1n1/perstn-run.sh 18` — `--preinit-probe --tier3
--clkreq-mode=periph --require-build=rc1-59-g`:
- **`--clkreq-mode=periph`** (new): mux gpio0[162] to the ADT-declared
  alt-function 2 (mirroring pinctrl-apple-gpio's pinmux: PERIPH field +
  INPUT_ENABLE) instead of the manual GPIO override, before the PERSTN
  cold reset and `pcie_init`.
- **5 s post-init LINKSTS watch** per active port (new) — catches
  late-converging training the 250 ms C poll would miss.
- Then tier dumps (Tier 3a auto-gated OFF — no phy_ip anywhere), LTSSM
  kick, and the **ECAM walk on a live boot for the first time in the
  numeric era**. NIC vendor/device ID in ECAM = goal.

Matrix: BUSY clears → link-training breakthrough → ECAM → NIC. Still
BUSY → RUN 19 candidates: PERST re-sequencing, longer settles, per-port
REFCLK setup (Linux `apple_pcie_setup_refclk` analog), `--no-clkreq`
(pure iBoot pin state). ECAM readable despite BUSY → port fabric alive,
training-only problem — high signal either way.
