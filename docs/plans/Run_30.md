# Plan — RUN 30: read-only probe of candidate CPU-start offsets (find the real M4 register)

## Context

RUN 29 **confirmed H5**: m1n1's assumed CPU-start register (`pmgr+0x34000`, inherited from M2/M3)
is **inert on M4**. After `--smp-start` writes m1n1's strobe, the block at `0x380734000` reads
`+0x0/+0x4 = 0x300` (bits 8,9 — unchanged across RUN 26/28/29) and `+0x8..+0x3c = 0`. The **running
core cpu6's bit is absent under every encoding at every offset** (per-cluster `1<<(4*cluster+core)` →
bit 4; flat `1<<cpu_id` → bit 6 — neither present anywhere). If `0x34000` were the real enable
register, cpu6 (which *is* running) would show as enabled somewhere. It doesn't → the register is
elsewhere. The run completed cleanly (pmgr-space reads don't SError, unlike RUN 28's ACC window).

**The decisive new lead (git-verified):** upstream m1n1 `main` **differentiated the M4 family's
CPU-start offset**. `git show upstream_source/main:src/smp.c`:
- **T6040 (M4 Pro/Max) → `CPU_START_OFF_T6031` = `0x88000`** (the M3-Max value) — *not* 0x34000.
- **T8132 / T8140 (our base M4) → still `CPU_START_OFF_T8112` = `0x34000`**, added by Yureka as a
  bare `case T8132:` in the T8112 group (commit `0a9302e`) with **no reasoning** — an unverified
  guess. It was never validated for T8132.

So a sibling M4 die uses `0x88000`, and our die's `0x34000` is an untested inheritance. The real
T8132 offset is unknown and **cannot be cribbed** from Linux (spin-table: cores pre-released by
iBoot; no `cpu-start` reg in the DT) or the ADT (no property names it). It must be found empirically.

**Decision confirmed with user:** RUN 30 = **read-only probe of candidate offsets first** (pin the
real register before spending the project's first reflash). Extend the CPU-start decode to dump a
*candidate* block and check whether it reflects **cpu6-running** (cpu6's bit set under a sensible
encoding). If one candidate cleanly shows cpu6 enabled, that's the real offset.

**Critical safety constraint (RUN 28 lesson):** reading a wrong/unmapped pmgr sub-region can throw
`SError`, which **desyncs the proxy UART and wedges m1n1** (needs a power-cycle) — `guarded()` does
NOT make it safe. Only `0x34000`+0x40 is *proven* AP-readable; `0x88000` etc. are unknown. Since a
single SError ends the session, RUN 30 probes **one candidate offset per boot** (via an argument),
reads **cpu6-first**, and aborts on the first wedge — so a wedge costs at most that one candidate,
not the whole campaign.

## Approach

### Parameterize `cpustart_decode` with a probe offset — `Scripts/m1n1/soc_bringup.py`

Change `cpustart_decode(buf)` → `cpustart_decode(buf, start_off=CPU_START_OFF_T8112)`:
- Compute `base = pmgr_reg + start_off`; log which offset is being probed.
- Keep the existing full-block dump (`+0x0..+0x40`, step 4) + the three-encoding per-word decode
  (`_decode_bits_to_cores`, already handles per-cluster / per-core / flat).
- **Add an explicit "does this block reflect cpu6-running?" verdict:** for each non-zero word, check
  whether the running-core set (just cpu6) is *exactly* represented under any one encoding (e.g. a
  word with only bit 6 set → flat match for cpu6; only bit 4 → per-cluster match). Print
  `MATCHES running set under <encoding>` when so — that's the signal the offset is real.
- **Harden the read loop:** unchanged structure (`guarded()` + `_read32_live` + `check_alive()`
  after each read, abort on wedge), but read the **very first word of the candidate block first**
  and, if it wedges, bail immediately with a clear "candidate offset 0xNNNNN SError'd — likely
  unmapped; power-cycle and try the next candidate" message. (This is the `check_alive`→`DumpAborted`
  path already present; just make the abort message name the offset so the log is self-documenting.)

Add `--cpustart-offset=<hex>` (default `0x34000`) to `build_argparser()`; parse it in `main()` (like
`--release-core`) and pass to `cpustart_decode`. Keep `--cpustart-decode` as the enable flag.

### RUN 30 dispatcher arm — `Scripts/m1n1/perstn-run.sh`

Add a `30)` arm (after `29)`), `RUNNER=soc_bringup.sh`, flags:
`--smp-start --smp-probe --cpustart-decode --cpustart-offset=0x88000 --require-build=rc1-60-g`.
(Probes the **T6040 offset first** — the strongest candidate. `--smp-start` first so the block
reflects a post-strobe state.) Add `30` to the two usage strings. The remaining candidates
(`0x30000`, `0x38000`, `0x54000`, `0x28000`) are tried by re-running with a different
`--cpustart-offset=…` (the arm documents them in a comment) — one per boot, power-cycling between.

### Docs (project convention)

- **`Scripts/m1n1/logs/29/findings.md`** — RUN 29 outcome: H5 confirmed (0x34000 inert; cpu6 bit
  absent everywhere); the upstream T6040=0x88000 split; RUN 30 = candidate-offset probe.
- **`docs/plans/Run_30.md`** — committed per-run plan doc (repo `Run_NN.md` style).

## Critical files

- **`Scripts/m1n1/soc_bringup.py`** — parameterize `cpustart_decode(buf, start_off=…)`; add the
  cpu6-match verdict + offset-named abort; new `--cpustart-offset`; wire into `main()`. Reuse
  `_decode_bits_to_cores`, `_cpu_nodes_by_reg`, `_read32_live`, `guarded`, `check_alive`, the
  `CPU_START_OFF_T8112`/`CPU_REG_*` constants.
- **`Scripts/m1n1/perstn-run.sh`** — new `30)` arm (probes `0x88000`) + usage strings.
- **`Scripts/m1n1/logs/29/findings.md`**, **`docs/plans/Run_30.md`** — new docs.
- Reference only: `m1n1/src/smp.c:18-23` (offset table), `smp.c:282-297` on upstream (T6040→0x88000
  split; the `case T8132:` guess), commit `0a9302e` (unverified T8132 offset). Eventual fix (RUN 31,
  reflash): add `CPU_START_OFF_T8132 = <confirmed offset>` + a dedicated `case T8132:`.

## Verification

Real hardware (M4 mini + m1n1 over UART); the **user** runs the live steps. No reflash (rc1-60-g).
**Power-cycle the M4 first** each run (a candidate offset may SError and wedge m1n1).

1. **Static (safe here):**
   - `python3 -c "import ast; ast.parse(open('Scripts/m1n1/soc_bringup.py').read())"`
   - `bash -n Scripts/m1n1/perstn-run.sh`
   - Confirm `perstn-run.sh 30` routes to `soc_bringup.sh` with `--cpustart-offset=0x88000`, and that
     `--cpustart-offset` parses (extract `build_argparser`+`parse_args`).
2. **Live:** `./Scripts/m1n1/perstn-run.sh 30` (probes `0x88000`). If inconclusive/wedged,
   power-cycle and re-run with `--cpustart-offset=0x30000` (then `0x38000`, `0x54000`, `0x28000`):
   `./Scripts/m1n1/perstn-run.sh 30 --cpustart-offset=0x30000`.
3. **Read** `/tmp/m4-recon/nic-runtime.txt`, keying on:
   - Did the candidate block read cleanly (no SError / `[flush:done]` reached, m1n1 alive)? A clean
     read of `0x88000` is itself informative (that region is mapped).
   - **Does any word in the candidate block reflect cpu6-running** (bit 6 flat, or bit 4 per-cluster)
     — the `MATCHES running set` verdict? If yes → that offset is the real CPU-start register.
   - Compare the candidate block's non-zero pattern to `0x34000`'s inert `0x300`.
4. **Outcome → RUN 31 (record in `logs/30/findings.md`):**
   - **A candidate block reflects cpu6-running** → real offset found → RUN 31 = reflash: add
     `CPU_START_OFF_T8132 = <that offset>` + `case T8132:` in `smp.c`, rebuild, test
     `smp_start_secondaries`; success = a secondary prints `OK`/`RVBAR entry`/`Started.`
   - **No candidate reflects cpu6, or all SError** → the release register isn't a simple pmgr strobe
     (SMC/AIC/mailbox, or a pre-strobe unlock) → deeper RE, or escalate with the assembled evidence
     (this is genuinely-unsolved territory — no m1n1 branch boots M4 secondaries).

## Non-goals for this run

**No writes** (read-only decode only), **one candidate offset per boot** (a wrong offset may SError),
no m1n1-source edits, no rebuild, no reflash. No ACC/CPM windows. No IOP boot, no `pcie_init`, no
phy_ip.
