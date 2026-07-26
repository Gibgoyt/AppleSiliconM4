# Plan — RUN 31: bounded pmgr-window scan for the M4 CPU-start register (last read-only shot)

## Context

The M4 (t8132 / j773g) secondary-CPU-release register is proving to be a **bootloader-private
detail** with no OS-visible declaration. This is the final read-only attempt to locate it by scanning
the proven-readable pmgr window; if it comes up empty, the mechanism isn't a simple AP-visible pmgr
strobe and the SMP hunt should stop.

**Where we are (RUNs 24–30):** cores are powered (PMGR `actual=0xf`); RVBAR is correct
(→ m1n1 `_vectors_start`) but sticky-locked (a red herring — the boot core runs locked); m1n1's
assumed CPU-start register `pmgr+0x34000` is **inert** (RUN 29: the running core cpu6's bit absent
under every encoding at every offset; `+0x0/+0x4 = 0x300` unchanged across runs); the sibling-die
T6040 offset `0x88000` is **mapped but all-zero** (RUN 30 — eliminated, clean read, no cpu6 match).

**Why blind offset-guessing is a dead end (research-confirmed):**
- **Linux t8132.dtsi uses `enable-method = "spin-table"`, `cpu-release-addr = <0 0>`** for every CPU
  — Linux does NOT touch a pmgr CPU-start register; the cores are released by the **bootloader**
  (iBoot/m1n1) and Linux only writes the spin-table release address.
- **`t8132-pmgr.dtsi` declares only power-state gates** (`ps_*` at 0x108, 0x110, …) — **no
  cpu-start reg**. The CPU-start block is a raw MMIO region m1n1's hardcoded `CPU_START_OFF` table
  reverse-engineered per-SoC; **t8132's `0x34000` was never RE'd** (added as a bare `case T8132:`
  guess, commit `0a9302e`).
- **Not AIC** (AIC handles IPIs, not CPU start). **`reg-private` (0x21x0100000) is CPU-local space**
  — the same family as the acc-impl window that SError'd, so not safely AP-readable and not the
  start register.
- The remaining offset guesses (`0x30000`, `0x38000`, `0x54000`, `0x28000`) are other SoCs' values
  with **no t8132 basis**.

**Decision confirmed with user:** take the **one remaining non-blind read-only shot** — a bounded
scan of the proven-readable pmgr window for the word that actually reflects core-enable state (equals
`0x40` = flat cpu6, or `0x10` = per-cluster cpu6, or a sensible core-mask). If found, that's the
register (→ reflash). If not, the release isn't an AP-visible pmgr strobe → stop guessing, escalate.

**Critical safety (RUN 28 lesson):** a wrong/unmapped offset can throw `SError`, which desyncs the
proxy UART and **wedges m1n1** (power-cycle) — `guarded()` does not prevent it. So the scan covers
**curated, plausibly-mapped offsets only**, reads the **first word of each region first**, and
**aborts the entire scan on the first SError**, naming the offset reached. A wedge costs the scan but
is bounded and attributable.

## Approach

### New `cpustart_scan(buf)` — read-only, curated bounded scan — `Scripts/m1n1/soc_bringup.py`

A multi-region scan that reuses `cpustart_decode`'s per-word decode + cpu6-match logic
(`_decode_bits_to_cores`, the `flat_mask`/`pcl_mask` expected values). Scans a **curated list of
pmgr sub-regions**, each a small bounded block (`+0x0..+0x40`, step 4):

- **The known SoC `CPU_START_OFF` offsets** (each is a *real register bank* on some Apple SoC — the
  most likely places a CPU-start block actually lives): `0x30000, 0x34000, 0x38000, 0x54000, 0x88000,
  0x28000, 0xd4000`.
- **A bounded neighborhood sweep around `0x34000`** (where we know there's live-looking data — the
  persistent `0x300`): `0x30000..0x3C000` in `0x1000` steps, reading the first word of each 4 KB
  page. This catches a register that moved a few KB from the M2 location.

For each region: read the **first word under `guarded()` + `check_alive()`**; if it wedges, write
`[ABORT] scan SError'd at pmgr+0xNNNNN — power-cycle; stop (release not an AP-visible pmgr strobe in
this range)` and **return** (don't probe further — the proxy is desynced). Otherwise dump the block,
decode each non-zero word, and flag any word that **equals `flat_mask` (0x40) or `pcl_mask` (0x10)**,
or whose set bits form a plausible single-core mask, as a **`<== CPU-start CANDIDATE`**. Track and
summarize all candidates at the end.

Keep the run bounded: cap total reads (e.g. ≤ ~200 words) and log the cap. This is a *scan*, so it
prints a compact per-region summary (region base + any non-zero words + any candidate), not every
zero word.

Add `--cpustart-scan` (read-only) to `build_argparser()`; wire into `main()` after
`--cpustart-decode`; extend the "no probe selected" guard.

> Note: order the curated offsets so the **most-likely-mapped** (0x34000, then its neighbors, then
> the other known-SoC values) come first — so if an SError aborts the scan, we've already covered the
> highest-value offsets. `0x34000`/`0x88000` are already proven-safe reads, so lead with those and
> their neighbors before venturing to untested offsets like `0xd4000`.

### RUN 31 dispatcher arm — `Scripts/m1n1/perstn-run.sh`

Add a `31)` arm (after `30)`), `RUNNER=soc_bringup.sh`, flags
`--smp-start --smp-probe --cpustart-scan --require-build=rc1-60-g`. Add `31` to the two usage
strings.

### Docs (project convention)

- **`Scripts/m1n1/logs/30/findings.md`** — RUN 30 outcome (0x88000 eliminated); the spin-table /
  bootloader-private / no-OS-declaration research verdict; RUN 31 = the bounded scan as the last
  read-only shot, with the explicit fork (found → reflash; not-found → escalate/reassess).
- **`docs/plans/Run_31.md`** — committed per-run plan doc.

## Critical files

- **`Scripts/m1n1/soc_bringup.py`** — new `cpustart_scan()` + `--cpustart-scan`; wire into `main()`.
  Reuse `_decode_bits_to_cores`, `_cpu_nodes_by_reg`, `_read32_live`, `guarded`, `check_alive`, and
  the running-set mask logic from `cpustart_decode`.
- **`Scripts/m1n1/perstn-run.sh`** — new `31)` arm + usage strings.
- **`Scripts/m1n1/logs/30/findings.md`**, **`docs/plans/Run_31.md`** — new docs.
- Reference only: `m1n1/src/smp.c:18-23` (offset table), Linux `t8132.dtsi`
  (`enable-method="spin-table"`), `t8132-pmgr.dtsi` (ps-gates only, no cpu-start) — the evidence the
  register is bootloader-private.

## Verification

Real hardware (M4 mini + m1n1 over UART); the **user** runs the live steps. No reflash (rc1-60-g).
**Power-cycle the M4 first.**

1. **Static (safe here):**
   - `python3 -c "import ast; ast.parse(open('Scripts/m1n1/soc_bringup.py').read())"`
   - `bash -n Scripts/m1n1/perstn-run.sh`
   - Confirm `perstn-run.sh 31` routes to `soc_bringup.sh` with `--cpustart-scan`, and it parses.
2. **Live:** `./Scripts/m1n1/perstn-run.sh 31`.
3. **Read** `/tmp/m4-recon/nic-runtime.txt`, keying on:
   - Did the scan complete (`[flush:done]`, m1n1 alive) or abort on an SError (which offset)?
   - **Any `<== CPU-start CANDIDATE`** — a word equal to `0x40` (flat cpu6) or `0x10` (per-cluster
     cpu6), or a plausible single-core mask — and at which pmgr offset?
   - The per-region non-zero summary (what live-looking data exists in pmgr space besides the inert
     0x34000 `0x300`).
4. **Outcome → next step (record in `logs/31/findings.md`):**
   - **A candidate word reflecting cpu6 found** → real offset located → RUN 32 = reflash smp.c with
     `CPU_START_OFF_T8132 = <that offset>` + a dedicated `case T8132:`, rebuild, test
     `smp_start_secondaries`; success = a secondary prints `OK`/`RVBAR entry`/`Started.`
   - **No candidate anywhere (or scan SError-aborts)** → the M4 release is **not an AP-visible pmgr
     strobe** (bootloader-private, possibly SMC/iBoot-internal). **Stop the offset hunt.** Record the
     honest state and either (a) reassess whether SMP is required for the original PCIe goal, or
     (b) escalate to Asahi/m1n1 developers with the full RUN 24–31 evidence. This is the pre-agreed
     fork.

## Non-goals for this run

**No writes** (read-only scan only), curated/bounded offsets with abort-on-SError, no m1n1-source
edits, no rebuild, no reflash. No ACC/CPM/reg-private (CPU-local, SError-prone) windows. No IOP boot,
no `pcie_init`, no phy_ip. This is explicitly the **last** read-only offset-search run — if it finds
nothing, the plan is to stop and escalate, not to keep guessing.
