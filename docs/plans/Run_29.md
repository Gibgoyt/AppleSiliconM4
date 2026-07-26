# Plan — RUN 29: decode the live CPU-start block to test the wrong-offset hypothesis (read-only)

## Context

Two agents (log analysis + a full m1n1 branch-diff) have narrowed the M4 (t8132 / j773g) SMP wall to
one leading hypothesis that is checkable **read-only, no reflash**.

**What RUN 28 established** (logs `Scripts/m1n1/logs/28/`):
- **`--enable-core-parse` cleanly confirmed** the lead: every `/cpus` node has
  `function-enable_core = 138:Core(1<<cpu_id)` (cpu0=0x1 … cpu9=0x200), phandle 138 = the PMGR node
  (`pmgr1,t8132`). This is iBoot's core-release recipe, a **flat per-core bitmask** — and m1n1's
  `smp_start_cpu` never invokes it; it writes the legacy `pmgr+0x34000` strobe with a **per-cluster
  `1<<core`** encoding (a *different* encoding).
- **`--core-diff-scan` hit a hard `Exception: SError`** on the very first read of the per-cluster
  `acc-impl-reg` window (`0x211F00000`), which **desynced the proxy UART and wedged m1n1** (needs a
  power-cycle). So the ACC/CPM per-core "impl" windows are **not plain AP-MMIO** (they're
  CPU-local / system-register space) — the differential-MMIO approach to those windows is dead, and
  an SError is worse than an AXI stall (it corrupts proxy framing even under `guarded()`).

**What the branch-diff proved (decisive):** the M4 SMP fix is **NOT upstream.** Our `t8132-pcie`
branch's `smp_start_cpu` is already functionally equal to — actually *stricter* than — upstream's
M4 path (`!cyc_ovrd` == upstream's `!apple_sysregs_unlocked`; we keep a `return` upstream dropped).
`features_m4` is identically `// XXX figure out what features are actually available on M4` on every
branch (`main`, `m4-integration`, `yuyuyureka/*`); `start.S`/`startup.c` are byte-identical; and
there is **no sysreg/RVBAR unlock sequence anywhere in the tree**. A rebase/cherry-pick would change
nothing observable. (Two upstream commits — `954f80c` mmu_secondary_setup dsb+invalidate, `707d564`
L2C_ERR gating — are worth taking *later*, but both fire only *after* a core is already running, so
neither explains our "zero output" symptom.)

**The leading hypothesis (H5):** m1n1's CPU-start strobe offset **`0x34000` (`CPU_START_OFF_T8112`)
is an unverified M2/M3 guess for T8132** (`smp.c:289-291` lumps T8132 into the T8112 case). We have
**direct evidence M4 relocated per-cluster MMIO** (the acc-impl SError). If M4 likewise moved the
CPU-start block, m1n1's strobe lands on the wrong address → the reset is never released → the core
emits zero output — **exactly our symptom** (no `OK`, no `RVBAR entry on secondary CPU`). Supporting
hint: RUN 26/28 read the block at `0x380734000` as `+0x0/+0x4 = 0x300` (bits 8,9). Under m1n1's
`1<<(4*cluster+core)` formula, bits 8,9 map to cluster2 core0/1 — **which don't exist on a 2-cluster
part** — so the value already looks inconsistent with the assumed layout/encoding.

**Decision confirmed with user:** RUN 29 = **read-only decode of the live CPU-start block** (all in
proven-readable pmgr space; NO ACC/CPM/SError windows). Find where the *running* core's enable/start
bit actually is, and whether the encoding is per-cluster or the flat `1<<cpu_id` the `Core()` recipe
uses. That localizes the real offset before any reflash.

## Approach

### New `cpustart_decode(buf)` — read-only, pmgr-space only — `Scripts/m1n1/soc_bringup.py`

A dedicated, deeper decode of the CPU-start block than `smp_probe`'s 4-word peek. Everything stays
in the pmgr reg window (`0x380700000`, proven readable — gates + the block itself read fine in
RUN 26/28); **it never touches the ACC/CPM windows that SError.** Reuses `smp_probe`'s derivation
(`pmgr reg[0] + CPU_START_OFF_T8112`) and the `m4_common` primitives.

1. **Full block dump.** Resolve `cpu_start_base = pmgr_reg + 0x34000`. Read a bounded window
   (e.g. `+0x0 .. +0x40`, step 4 — the block is small; m1n1 only uses `+0x0/+0x4/+0x8/+0xc`) under
   `guarded()` + `check_alive()` after each read, aborting on any wedge. (These are known-safe reads;
   the guard is belt-and-suspenders.)
2. **Decode against the live core set.** From `/cpus`, build the running/waiting sets and each core's
   `(die, cluster, core, cpu_id)`. For each non-zero block word, decode its set bits under **both**
   candidate encodings and report which cores they'd correspond to:
   - per-cluster m1n1 encoding: bit `4*cluster+core` (what `smp.c:168` uses at `+0x4`), and bit
     `core` (what `smp.c:171` uses at `+0x8+4*cluster`).
   - flat encoding: bit `cpu_id` (what `function-enable_core = Core(1<<cpu_id)` uses).
   Flag the interpretation under which the block's set bits **match the actually-running cores**
   (only cpu6 running). E.g. if some word has exactly bit 6 set → flat `1<<cpu_id` for cpu6 → the
   real enable register uses the flat encoding and m1n1's per-cluster strobe is wrong.
3. **Strobe-delta (optional, still read-only of the block).** Because RUN 29's dispatcher runs
   `--smp-start` first (which makes m1n1 write its strobe), the block already reflects a
   post-strobe state. Also dump the same window and **diff against the pre-strobe expectation**
   noted from m1n1's source (what m1n1 *intended* to write: `+0x4 |= 1<<(4*cluster+core)` and
   `+0x8+4*cluster |= 1<<core` for each attempted secondary). If m1n1's intended bits are NOT
   present in the readback, the writes aren't landing at this offset → strong wrong-offset signal.
4. **Verdict block.** "Running-core bit found at `+0xNN` under <encoding> → that word/encoding is the
   real enable register → RUN 30 candidate: `CPU_START_OFF_T8132` and/or flat-bitmask strobe." vs
   "m1n1's intended strobe bits are present but cores still don't start → offset is right, the
   blocker is elsewhere (revisit)." vs "block reads all-zero/garbage → offset likely wrong → widen."

Add `--cpustart-decode` (read-only) to `build_argparser()`; wire into `main()` after `smp_probe`;
extend the "no probe selected" guard.

### Harden the probe against SError (so RUN 29 can't repeat RUN 28's wedge)

RUN 28 wedged because an SError desyncs the proxy even under `guarded()`. RUN 29 avoids the
SError-ing windows entirely (pmgr-space only). Additionally, add a short module comment in
`soc_bringup.py` documenting the RUN-28 lesson (SError ≠ AXI-stall; `guarded()` does not make an
SError-prone MMIO safe; only touch windows proven AP-readable), so future probes don't reintroduce
ACC/CPM reads.

### RUN 29 dispatcher arm — `Scripts/m1n1/perstn-run.sh`

Add a `29)` arm (after `28)`), `RUNNER=soc_bringup.sh`, flags
`--smp-start --smp-probe --cpustart-decode --require-build=rc1-60-g`. **Do NOT include
`--core-diff-scan`** (it SErrors/wedges). Add `29` to the two usage strings.

### Docs (project convention)

- **`Scripts/m1n1/logs/28/findings.md`** — RUN 28 outcome: enable_core recipe confirmed; ACC-window
  SError/wedge; the branch-diff verdict (fix not upstream); H5 (wrong CPU-start offset); RUN 29 =
  CPU-start decode.
- **`docs/plans/Run_29.md`** — committed per-run plan doc (repo `Run_NN.md` style, matches Run_28.md).

## Critical files

- **`Scripts/m1n1/soc_bringup.py`** — new `cpustart_decode()` + `--cpustart-decode`; wire into
  `main()`; SError-lesson comment. Reuse `smp_probe`'s address derivation, `_read32_live`,
  `guarded`, `check_alive`, `_cpu_nodes_by_reg`, `log`.
- **`Scripts/m1n1/perstn-run.sh`** — new `29)` arm + usage strings.
- **`Scripts/m1n1/logs/28/findings.md`**, **`docs/plans/Run_29.md`** — new docs.
- Reference only (RUN 30, the eventual fix — needs reflash): `m1n1/src/smp.c:18-23` (CPU_START_OFF
  table), `smp.c:287-303` (the `case T8132` that reuses 0x34000), `smp.c:163-171` (the strobe writes
  + encoding). Opportunistic later cherry-picks once cores boot: `954f80c` (mmu_secondary_setup),
  `707d564` (L2C_ERR gating, adapt `apple_sysregs_unlocked`→`cyc_ovrd`).

## Verification

Real hardware (M4 mini + m1n1 over UART); the **user** runs the live steps. No reflash (rc1-60-g).
**Power-cycle the M4 first** (RUN 28 left m1n1 wedged via the SError desync).

1. **Static (safe here):**
   - `python3 -c "import ast; ast.parse(open('Scripts/m1n1/soc_bringup.py').read())"`
   - `bash -n Scripts/m1n1/perstn-run.sh`
   - Confirm `perstn-run.sh 29` routes to `soc_bringup.sh` with the expected flags (no
     `--core-diff-scan`), and `--cpustart-decode` parses.
2. **Live:** `./Scripts/m1n1/perstn-run.sh 29`.
3. **Read** `/tmp/m4-recon/nic-runtime.txt`, keying on:
   - `--cpustart-decode`: the full `0x380734000` block dump + the per-word decode. **Under which
     encoding do the set bits match the running core (cpu6)?** Are m1n1's *intended* strobe bits
     (`+0x4` bit for `4*cluster+core`, `+0x8+4*cluster` bit for `core`) actually present after
     `--smp-start`? A flat `1<<cpu_id` match, or absent intended bits, = wrong-offset/encoding
     confirmation.
   - Run reaches `[flush:done]`, m1n1 stays alive (no SError — we avoid the ACC windows).
4. **Outcome → RUN 30 (record in `logs/29/findings.md`):**
   - **Running-core bit found under the flat `1<<cpu_id` encoding, or m1n1's intended per-cluster
     bits absent** → the strobe offset/encoding is wrong for M4 → RUN 30 = a reflash experiment:
     add `CPU_START_OFF_T8132` and/or a flat-bitmask strobe in `smp.c`, rebuild, reflash, re-test;
     success = a secondary prints `OK` / `RVBAR entry on secondary CPU` / `Started.`
   - **m1n1's intended bits ARE present at 0x34000 yet cores don't start** → offset is right, blocker
     is elsewhere (e.g. an additional per-core deassert the `Core` recipe does) → deeper RE.

## Non-goals for this run

**No writes** (read-only decode only), **no ACC/CPM/SError windows** (pmgr-space reads only), no
m1n1-source edits, no rebuild, no reflash, no cherry-picks. No IOP boot, no `pcie_init`, no phy_ip.
