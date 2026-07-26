# Plan — RUN 28: find the M4 core-release register via `function-enable_core` differential scan (read-only)

## Context

RUN 27 eliminated the RVBAR-lock hypothesis and surfaced the real lead. The M4 (t8132 / j773g)
SMP wall now has a concrete, testable next step that needs **no reflash**.

**What RUN 27 proved** (logs pasted; to be saved at `Scripts/m1n1/logs/27/`):
- Writing `_vectors_start` back into a secondary's RVBAR **did not clear `RVBAR_LOCK`** — the lock
  is **sticky-until-reset** (`[post] RVBAR=…001 (LOCK=True)`). AND the boot core cpu6 runs fine
  with `LOCK=True`. So **the RVBAR lock is a red herring** for release — not the blocker. (This is
  the "LOCK stayed set → rethink" branch RUN 27's own matrix anticipated.)
- The address logic is fine: RVBAR = `0x10003dfc000` = `m1n1 base` = `_vectors_start` (relocated
  this boot). CPU-start offset `0x34000` is correct for t8132 (research-confirmed, = T8112's).

**The lead (verified in ADT + source):** every `/cpus/cpuN` node carries
`function-enable_core = 138:Core(<core-bitmask>)` (cpu0=0x1, cpu1=0x2, … cpu6=0x40, cpu7=0x80…).
Phandle 138 is the **PMGR node** (`compatible=[pmgr1,t8132]`, `m4_recon/adt.txt:6886-6889`). So M4
core-enable is a **PMGR device-function** — and **m1n1's SMP path never invokes it**; it writes only
the legacy `pmgr+0x34000` strobe (`smp.c:168/171`). The boot core cpu6 was released by iBoot via
this `function-enable_core` recipe; the 9 secondaries only ever get the strobe. **Hypothesis H4:
M4 secondaries must be released via the PMGR `Core(bitmask)` recipe, not the legacy strobe.** A
concrete discrepancy supports it: the `Core()` arg is a **flat per-core bit** (cpu6=0x40), whereas
m1n1's strobe writes per-cluster `1<<core` — different encodings.

**Why a scan, not a code fix:** the 'Core' FourCC function has **no register-level implementation in
m1n1 or Asahi Linux** — `m1n1/src/pmgr.c` only models `clock-gates`→PS registers and has no generic
`(phandle, fourcc, args)` evaluator; Linux's `smp_spin_table.c` assumes the cores were *already*
released by iBoot/m1n1 (`cpu-release-addr` spin-table). The recipe lives in iBoot/SMC firmware, so
the register **cannot be cribbed from source** — it must be found by a **live running-vs-waiting
differential scan**. cpu6 (running) and cpu7 (waiting) are in the **same cluster (1)** and share the
cluster windows `acc-impl-reg=0x211F00000` and `cpm-impl-reg=0x211E40000` — a per-core enable/run bit
there will read set for cpu6, clear for cpu7. That's the register candidate.

**Decision confirmed with user:** RUN 28 is **read-only** — parse + differential scan only, **no
writes**. The gated write test (to actually release a core) is a *later* run, once the scan gives a
candidate. (The write target is unknown until the scan runs.)

## Approach

Two new read-only functions in `Scripts/m1n1/soc_bringup.py`, reusing the `smp_probe` /
`smp_diag` patterns and the `m4_common` primitives (`_read32_live`, `guarded`, `check_alive`,
`_load_pmgr_devices`, `_read_pmgr_gate_state`, `log`, `try_`).

### 1. `enable_core_parse(buf)` — pure ADT, zero MMIO (always safe)

For each `/cpus` node: `fn = cpu.getprop("function-enable_core")` → a parsed `Function`
(`adt.py:28-32` — `.phandle`, `.name`, `.args`). Assert `fn.phandle == 138`, resolve 138 → confirm
it's the pmgr node (`compatible` contains `pmgr1,t8132`), assert `fn.name == "Core"`, print
`fn.args[0]` (the core bitmask) and cross-check `(1 << cpu_id) == fn.args[0]`. Also dump
`function-cpu_idle` / `function-error_handler` for context. Emits a table confirming the
(phandle, name, args) tuple before any MMIO. Gated behind `--enable-core-parse`.

### 2. `core_diff_scan(buf, run_reg=0x100, wait_reg=0x101)` — read-only differential (the core of RUN 28)

Compare cpu6 (running, reg 0x100) vs cpu7 (waiting, reg 0x101), same cluster. Gated behind
`--core-diff-scan`, with `--run-core` / `--wait-core` overrides (default 0x100 / 0x101).

- **Gate precondition:** `_load_pmgr_devices`, confirm the cluster-1 CPU/ACC PMGR gate reads
  `actual==0xf` (ACTIVE) via `_read_pmgr_gate_state` (reuse the `smp_diag` cluster-gate scan,
  `soc_bringup.py:181-194`). If not ACTIVE → print and **abort** — never MMIO a non-active block.
- **Windows** (resolved per-node via `getprop`, like `smp_probe`):
  - `acc-impl-reg` (shared cluster window, base `0x211F00000`) — **primary suspect.** Scan bounded
    sample bands only (e.g. `0x0..0x200` and `0x2000..0x2200`), NOT the full 0x40088 span, to keep
    read-count and wedge-exposure low.
  - `cpm-impl-reg` (shared cluster window, `0x211E40000`) — second suspect. Same bounded bands.
  - `cpu-impl-reg + 0x100` (per-core; the status word m1n1 polls at `smp.c:223`,
    `read64(impl+0x100) & 0xff`) — the **running/stopped signal**: cpu6 alive vs cpu7 stopped.
    Guaranteed-informative, low risk (m1n1 reads it itself).
  - `coresight-reg` / `reg-private` offset 0 only — per-core controls (expected to differ; low
    signal).
- **Per read:** wrap in `guarded()`, read **cpu6 (proven-live) before cpu7** at each offset, and
  `check_alive()` after every read — **abort the whole scan on the first wedge** (mirror
  `smp_probe`'s abort, `soc_bringup.py:295-297`). cpu7's own per-core window is the only mild risk
  (a held-in-reset core's IMPL window *might* stall even when powered); the cpu6-first ordering makes
  any stall attributable.
- **Output:** a `offset | cpu6(run) | cpu7(wait) | DIFF?` table. **Flag shared-window (acc/cpm)
  offsets that differ as enable-bit candidates** — a bit set on the running core and clear on the
  waiting core is the release register. Rank: shared-window single-bit diff > shared-window
  multi-bit > per-core status word.

### 3. `main()` + dispatcher wiring

Add `--enable-core-parse` and `--core-diff-scan` (+ `--run-core`/`--wait-core`) to
`build_argparser()`; wire into `main()` after `smp_probe`; extend the "no probe selected" guard.
Both are **read-only** and safe to run together by default.

**`perstn-run.sh`**: add a `28)` arm (after `27)`), `RUNNER=soc_bringup.sh`, flags
`--smp-start --smp-probe --enable-core-parse --core-diff-scan --require-build=rc1-60-g`. Add `28`
to the two usage strings. (`--smp-start` first records the "Failed!" baseline on the same boot, so
cpu6-vs-cpu7 is a genuine running-vs-just-refused differential.)

### 4. Docs / findings (project convention)

- **`Scripts/m1n1/logs/27/findings.md`** — RUN 27 outcome: RVBAR lock sticky + non-blocking ⇒ red
  herring; the `function-enable_core`/PMGR-138 discovery; H4; RUN 28 = the differential scan.
- **`docs/plans/Run_28.md`** — the committed per-run plan doc, mirroring this plan in the repo's
  existing `docs/plans/Run_NN.md` style (matches `Run_27.md`, `Run_26.md`, …).

## Critical files

- **`Scripts/m1n1/soc_bringup.py`** — new `enable_core_parse()` + `core_diff_scan()` +
  `--enable-core-parse` / `--core-diff-scan` / `--run-core` / `--wait-core`; wire into `main()`.
  Reuse `smp_probe`'s address-derivation and `m4_common` helpers.
- **`Scripts/m1n1/perstn-run.sh`** — new `28)` arm + usage strings.
- **`Scripts/m1n1/logs/27/findings.md`**, **`docs/plans/Run_28.md`** — new docs.
- Reference only: `m1n1/proxyclient/m1n1/adt.py:28-32` (`Function` type; `getprop("function-…")`),
  `m4_recon/adt.txt:876-928` (cpu6/cpu7 window bases), `m1n1/src/smp.c:223` (the `impl+0x100`
  status word), `m1n1/src/pmgr.c` (no function-evaluator — why the register must be found live).

## Verification

Real hardware (M4 mini + m1n1 over UART); the **user** runs the live steps. No reflash (rc1-60-g).

1. **Static (safe here):**
   - `python3 -c "import ast; ast.parse(open('Scripts/m1n1/soc_bringup.py').read())"`
   - `bash -n Scripts/m1n1/perstn-run.sh`
   - Confirm `perstn-run.sh 28` routes to `soc_bringup.sh` with the expected flags, and that the new
     args parse (extract `build_argparser` + `parse_args`, as done for RUN 25/26/27).
2. **Live:** `./Scripts/m1n1/perstn-run.sh 28`.
3. **Read** `/tmp/m4-recon/nic-runtime.txt`, keying on:
   - `--enable-core-parse`: confirms every core's `function-enable_core` = phandle-138 PMGR
     `Core(1<<cpu_id)`. (Sanity that the ADT lead is real.)
   - `--core-diff-scan`: the diff table. **Is there a shared-window (acc/cpm) offset where a bit is
     SET for cpu6 and CLEAR for cpu7?** That's the enable-register candidate. And does
     `cpu-impl+0x100` differ (cpu6 alive vs cpu7 stopped) — the guaranteed "held in reset" signal?
   - exc_count deltas 0; scan wedge-free; run reaches `[flush:done]`.
4. **Outcome → RUN 29:**
   - **Clear shared-window enable-bit candidate found** → RUN 29 = gated write test
     (`--enable-core-write --cand-addr --cand-val`, operator-supplied): write the bit for cpu7 and
     watch TTY for `RVBAR entry on secondary CPU`. Marker → H4 confirmed → then the m1n1-source fix
     (invoke the enable_core recipe in `smp_start_cpu`) + reflash.
   - **No memory-mapped bit in the scanned windows** (recipe pokes SMC/AOP mailbox or a pmgr-node
     offset outside these windows) → the register isn't reachable read-only → RUN 29 becomes a
     reflash experiment (minimal m1n1 patch that instruments/replays iBoot's release path). The
     `impl+0x100` status differential still gives the definitive "cpu7 is held in reset" confirmation.

## Non-goals for this run

**No writes** (read-only scan only), no m1n1-source edits, no rebuild, no reflash. No IOP boot, no
`pcie_init`, no phy_ip. The scan reads only cluster ACC/CPM windows + per-core IMPL status of the
cpu6/cpu7 pair, all gated on the cluster PMGR gate being ACTIVE.
