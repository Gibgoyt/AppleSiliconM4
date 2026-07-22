# RUN 21 — PCIe link training: refclk-first PERST# re-sequence + LTSSM_START readback

## Context

m4-pcie bring-up in `AppleSiliconM4/` (Mac16,10 / J773g / t8132), poking hardware through
m1n1 to enumerate the 1GbE NIC (`lan-1gb`) on `pci-bridge2`. `pcie_init()` returns 0 and both
ports reach **LINKSTS BUSY** (port0 `0x8300020c`, port2 `0x83000204`) but never reach UP (bit0).

Runs so far falsified, each no-reflash:
- **RUN 18** (mux CLKREQ# to ADT alt-func 2): BUSY unchanged.
- **RUN 19** (per-port REFCLK REQ→ACK handshake on `phy_base+0x000`): handshake **succeeds**
  (REFCLK0ACK+REFCLK1ACK assert, `→0x3300067f`) — BUSY unchanged.
- **RUN 20** (replay m1n1's T602X LTSSM kick): mixed, and it produced the decisive clue.

### RUN 20 result — the sharp clue (verified against `logs/20/nic-runtime.txt`)

In the post-t602x dump (lines 934-944), three LTSSM config registers **latched**:
`ltssm+0x10=0x2`, `ltssm+0x1c=0x4`, `ltssm+0x20=0x2`; APPCLK bit8 cleared
(`0x00100101→0x00100001`); `+0x104` override took (`→0x7ffffff0`). **But**:

- **`ltssm+0x14` (LTSSM_START, m1n1 `pcie.c:861`) refused to latch** — written `0x1` twice
  (lines 794, 809), reads back **`0x00000000`** (line 935/942). The *config* regs take; the
  *start* bit will not set.
- `rc_base+0x3c` and `port_base+0x10` writes silently dropped (read back 0; lines 818/831).
- LINKSTS frozen BUSY through every step; the 5 s post-t602x watch says "still BUSY"; ECAM
  all vacant (`0xffffffff`), no class-0x02 device.

### Why START won't latch — the ordering bug (ADT-grounded)

The NIC bridge ADT (`m4_recon/adt.txt:2550-2566`, `nic-adt.txt`) declares
`t-refclk-to-perst = 100` and `perst-to-config = 100`: **refclk must be up, THEN PERST#
deasserts, THEN ~100 later config/LTSSM starts**. The bridge has *no* pwren / power_gate node —
only clkreq (gpio162) + perst (gpio165) — so there is no separate endpoint power rail we are
missing; endpoint power is just the SMC fabric keys (`gP0d`/`gP1a`), already set.

But perstn.py's current order is **backwards**: `deassert_perstn` (`perstn.py:366`) drives
PERST# high (released) *before* the refclk handshake and *before* pcie_init. So PERST# was
released with no refclk, refclk came up late, and the port/LTSSM sits in a substate that
rejects the START write. This is a canonical cause of LTSSM-never-converges and exactly fits
"config regs writable, START not."

## Goal

Reproduce the upstream `apple_pcie_setup_link` ordering as a **post-init re-sequence** (no
reflash): with refclk proven up, re-assert PERST#, cycle the internal port reset, deassert
PERST#, wait the ADT-declared settle, **then** write LTSSM_START and **read `ltssm+0x14` back**
as the primary instrument, then watch LINKSTS. The one observable that matters:
does `ltssm+0x14` flip `0x0 → 0x1`? If yes, START is finally latching and BUSY should follow.

## Approach

### 1. New re-sequence function — `Scripts/m1n1/perstn.py`

Add near the LTSSM helpers (after `watch_linksts`, ~`perstn.py:4650`):

```
def perst_resequence(apcie, buf, perstn_pin, settle_ms=100,
                     hold_ms=100, port_indices=None):
    """RUN 21: refclk-first PERST# re-sequence honoring the ADT
    t-refclk-to-perst / perst-to-config = 100 ordering, then write
    LTSSM_START (ltssm+0x14) and read it back -- the key instrument.
    Assumes setup_refclk already ran (phy_base+0x000 REFCLKEN set)."""
```

Behavior (per active port; NIC = port2):
1. Snapshot `phy_base+0x000` (confirm REFCLKEN still set) + LINKSTS (`_read32_live`,
   `_linksts_decode`).
2. Re-assert PERST#: `gpio_set_output(perstn_pin, 0, buf)` (reads back the pin reg → free
   gpio165 output-verify / endpoint-presence guard); hold `hold_ms`.
3. Cycle internal port reset: `clear32(port_base+0x82c, 0x1)` → 1 ms → `set32(port_base+0x82c,
   0x1)` (RUN 20 proved `+0x82c` latches), each read back.
4. Deassert PERST#: `gpio_set_output(perstn_pin, 1, buf)`.
5. **Settle `settle_ms` (=100) once** after PERST release (vs RUN 20's scattered 0.25 s waits).
6. Poll `port_base+0x208` BUSY(bit2)→0 for up to ~2 s (bounded loop like `setup_refclk`'s
   `_poll_ack`); log transitions via `_linksts_decode`.
7. Write LTSSM_START: `write32(ltssm+0x10,0x2); write32(ltssm+0x1c,0x4); set32(ltssm+0x20,0x2);
   write32(ltssm+0x14,0x1)`; **then `_read32_live(ltssm+0x14, ...)` — the primary result.**
8. Final LINKSTS snapshot.

Every proxy access guarded with `check_alive()`→`DumpAborted`, matching
`t602x_port_init_replay`/`setup_refclk`. All addresses are Tier-1/GPIO — proven reachable in
RUN 20 (exc_count delta 0); phy_ip / phy_extra / ctrl_lo stay gated OFF.

### 2. Argparse + main() wiring — `Scripts/m1n1/perstn.py`

- Add `--perst-resequence` (store_true) and `--perst-settle-ms` (int, default 100), near
  `--setup-refclk` (~`perstn.py:5480`).
- In main(), call it inside `if pcie_init_ok:` **after `setup_refclk(post)`** and **after the
  post-init LINKSTS watch**, guarded by `liveness_gate("perst-resequence")` + `flush(
  "perst-resequence")`, passing `perstn_pin` (resolved at `perstn.py:5857`) — thread it down
  via a local since main()'s pin vars are in scope. Then let the existing post-t602x-style
  LINKSTS watch / ECAM walk report the result (reuse `watch_linksts(..., label="post-perst")`).

### 3. New RUN 21 dispatcher arm — `Scripts/m1n1/perstn-run.sh`

Add `21)` before `*)` (~line 1265) + `21` in the usage string:

```
    21)
        # RUN 21: link training, part 4. RUN 20 landed the LTSSM config
        # writes (ltssm+0x10/0x1c/0x20) but LTSSM_START (ltssm+0x14) refused
        # to latch (read back 0) and LINKSTS stayed BUSY. Root cause: PERST#
        # is deasserted BEFORE refclk is up, violating the ADT's
        # t-refclk-to-perst=100 / perst-to-config=100 ordering, so the port
        # rejects START. Re-sequence refclk-first: setup_refclk, then
        # re-assert PERST#, cycle +0x82c, deassert PERST#, 100 ms settle,
        # poll BUSY->0, write LTSSM_START and READ ltssm+0x14 back (the key
        # instrument). Pin mux kept; phy_extra probes stay gated.
        #
        # Matrix: ltssm+0x14 latches 0x1 / BUSY clears -> LINK TRAINS ->
        # ECAM finds the NIC. START still 0 after correct ordering -> START
        # is gated deeper than reset timing; RUN 22 = patched m1n1 (in-window
        # T602X writes) or --no-clkreq A/B. port2 LINKSTS never distinguishes
        # from a dead port -> endpoint-presence axis (RUN 22 diagnostics).
        FLAGS=(--preinit-probe
               --tier3
               --clkreq-mode=periph
               --setup-refclk=both
               --perst-resequence
               --require-build=rc1-59-g)
        ;;
```

(Deliberately drops `--t602x-init` — RUN 20 showed the full replay adds noise and its
`rc_base+0x3c`/`port+0x10` writes drop anyway. RUN 21 isolates the ordering variable.)

## Critical files

- `Scripts/m1n1/perstn.py` — new `perst_resequence()` (~4650); argparse `--perst-resequence`
  / `--perst-settle-ms` (~5480); main() call after `setup_refclk(post)` + post-init watch
  (~6135). Reuse `gpio_set_output` (344), `setup_refclk` (4650-ish), `watch_linksts` (4613),
  `_read32_live`/`_linksts_decode`, `check_alive`/`DumpAborted`, `liveness_gate`.
- `Scripts/m1n1/perstn-run.sh` — new `21)` arm (~1265) + usage string.
- Reference only: `/home/ahmed/Projects/C/embedded/m1n1/src/pcie.c:754-889` (T602X gating,
  LTSSM_START = `write32(ltssm+0x14,0x1)` @861), `m4_recon/adt.txt:2550-2566` (ADT timing +
  no-pwren), `docs/ref-asahi-t8132-pcie.md`, `Scripts/m1n1/logs/20/nic-runtime.txt`.

Follow-up (not this change): the owed per-run logs `Scripts/m1n1/logs/{18,19,20}/findings.md`.

## Verification

Real hardware (M4 mini + m1n1 over UART) — the user runs it:

1. **Static**: `python3 -c "import ast; ast.parse(open('Scripts/m1n1/perstn.py').read())"`,
   `bash -n Scripts/m1n1/perstn-run.sh`, confirm `21)` routes and the new flags parse.
2. **Live**: `./Scripts/m1n1/perstn-run.sh 21`.
3. **Read** `/tmp/m4-recon/nic-runtime.txt`, keying on — in order of importance:
   - **`ltssm+0x14` readback after START** in the perst-resequence section: `0x1` (latched!)
     vs `0x0` (still rejected). This is the experiment.
   - the `post-perst` LINKSTS watch: `BUSY CLEARED` vs `still BUSY`.
   - port2-vs-port0 LINKSTS delta (does port2 gain the bit3 port0 has?).
   - if BUSY clears → ECAM walk should print the NIC VID:DID (class 0x02).
4. **Outcome matrix** (record in `logs/20/findings.md`):
   - `ltssm+0x14`→`0x1` and/or BUSY clears → START latched; link trains → NIC enumerates.
   - `ltssm+0x14` still `0x0` after correct ordering → START gated deeper than reset timing
     → RUN 22 = patched m1n1 running the T602X `rc_base+0x3c`+`port+0x10`+kick *in-window*
     during pcie_init (bisected, excluding the SError-triggering phy_ip writes; reflash), or
     `--no-clkreq` A/B control.
   - port2 stays indistinguishable from a dead port even with correct ordering → endpoint-
     presence axis → RUN 22 = SMC key readback + gpio165 verify + wiring/board check.
   - any write wedges m1n1 → all addresses were RUN-20-safe; investigate the regression.
