# Plan — Split out `soc_bringup.{sh,py}` for the SoC-first pivot (RUN 25+)

## Context

We are bringing up PCIe on a **t8132 / j773 Apple Silicon Mac mini M4 (base)** over m1n1/UART.
After six falsified single-core PCIe runs (18–23), RUN 24 executed the strategic pivot: **bring
the whole SoC up before touching PCIe.** RUN 24 was pure read-only recon and its logs
(`Scripts/m1n1/logs/24/{run.log,nic-runtime.txt}`) are decisive:

- **SMP: 9/9 secondary cores REFUSED to start.** Every `Starting CPU N (d:c:c)... Failed!`
  (run.log 131–142). This is the primary, SoC-level blocker — upstream of PCIe.
- **SoC recon:** `acio-cpu0/1/3` (roles ACIO0/1/3, `iop,mxwrap-acio`) gates 338/339/341 are
  **VIRTUAL / OFF** (nic-runtime.txt 77–124). All six apcie gates OFF/virtual-off; the real one,
  **gate 151 `APCIE_PHY_SW` = actual 0x4 (OFF)**, and its parent **gate 150 `APCIE_SYS_ST` =
  0x0 (hard power-gated)**. The `phy_ip`/CIO3-PLL window is provably unreachable until 151 powers.
- **LTSSM harvest:** both ports BUSY, LINKSTS constant, **bit3 set on port0 / clear on port2**;
  RC is **cycling but stuck (no link-up)**. exc_count delta = 0 everywhere (recon protocol clean).
- ECAM walk empty (0xffffffff); SMC apcie power readback mismatches (wrote 0x800001, reads 0x0).

**Outcome per run_24.md matrix:** "secondary cores refuse → SoC bring-up problem is real and
upstream of PCIe → RUN 25 = fix core startup first" — with the ACIO-IOP-owns-the-PHY theory still
standing (ACIO gates OFF, RC not linking) but not yet proven (its rtkit mailbox was never read).

**This change:** `perstn.py` is **6,717 lines** and PCIe-focused. The SoC-first work needs its own
home. We create `Scripts/m1n1/soc_bringup.{sh,py}` as the script we run **from RUN 25 onward** for
SMP + companion-IOP bring-up, extract the shared low-level primitives into a common module so both
scripts reuse them without duplication, and leave `perstn.py` as the PCIe-init/endpoint script that
`soc_bringup` can hand off to. RUN 25 itself is **scaffold + read-only recon only** (no IOP boot) —
gate the actual ACIO boot on what this recon finds, exactly as RUN 24 gated on its recon.

Decisions confirmed with the user: (1) extract shared primitives to a new `m4_common.py`;
(2) RUN 25 = scaffold + recon only, no IOP boot yet.

## Approach

### 1. New shared module — `Scripts/m1n1/m4_common.py`

Move the connection preamble + the ~15 low-level primitives that are currently inline near the top
of `perstn.py` into a new `m4_common.py`. This module owns the `m1n1.setup` import (so importing it
establishes `u`, `p`, `iface`) and re-exports the primitives. Contents (lifted verbatim from
`perstn.py`, keeping behavior identical):

- **Setup/preamble** — the `sys.path` appends + `from m1n1.setup import *`, `from m1n1.fw.smc
  import SMCClient`, `from m1n1.proxy import GUARD` (`perstn.py:51–58`). Expose `u, p, iface, GUARD,
  SMCClient` as module attributes.
- **Logging/flush** — `log()` (105), `try_()` (109), `flush_partial_log()` (240).
- **Liveness/guard** — `guarded()` (118), `check_alive()` (186), `check_alive_fast()` (214).
- **MMIO read** — `_read32_live()` (649).
- **PMGR gate state** — `_decode_pmgr_name()` (952), `_decode_ps_state()` (991),
  `_read_pmgr_gate_state()` (1012), `_load_pmgr_devices()` (1062).
- **ADT helper** — `_adt_compat_str()` (4898), `_IOP_COMPAT_MARKERS` (4894).
- **Build guard** — `_usb_product_string()` (83) + a small `require_build(substr)` helper wrapping
  the check at `perstn.py:6166–6183` (both scripts need the stale-binary guard).

Then in **`perstn.py`**: delete those inline defs and replace with
`from m4_common import (log, try_, guarded, check_alive, check_alive_fast, _read32_live,
_load_pmgr_devices, _read_pmgr_gate_state, _decode_pmgr_name, _decode_ps_state, flush_partial_log,
_adt_compat_str, _IOP_COMPAT_MARKERS, _usb_product_string, u, p, iface, GUARD, SMCClient, ...)`.
Keep `perstn.py`'s own PCIe-specific code unchanged. Net: `perstn.py` shrinks by ~200 lines and both
scripts share one copy of the primitives (the user's "too long" concern, addressed structurally).

> Note on module-level side effects: `from m1n1.setup import *` opens the UART on import. Because
> only ONE script runs per invocation and both import `m4_common` (not each other), there is exactly
> one connection per run — no double-open. Verify `perstn.py` still connects once after the edit.

### 2. New script — `Scripts/m1n1/soc_bringup.py`

A focused, self-contained SoC-bring-up script. Imports everything low-level from `m4_common`; owns
only SoC-recon logic. Structure mirrors `perstn.py`'s `main()` scaffold (argparse → build guard →
`out/nic-runtime.txt` + `buf` + `flush(tag)` closure, per `perstn.py:6185–6216`):

- **`--smp-start`** — `p.smp_start_secondaries()` inside `try_`, logging the `Starting CPU N...`
  TTY lines (move the block from `perstn.py:6207–6212`).
- **`soc_recon(buf)`** — move the existing function verbatim (`perstn.py:4911–4970`); it already
  uses only `m4_common` primitives (`_load_pmgr_devices`, `_read_pmgr_gate_state`,
  `_adt_compat_str`, `_IOP_COMPAT_MARKERS`).
- **NEW `smp_diag(buf)` (read-only)** — the RUN-25 addition: after the SMP-start attempt, read *why*
  cores refuse. Per refused core, read (guarded, exc-safe) its PMGR CPU-gate / reset-manager state
  from the ADT `cpus` nodes + PMGR — is the cluster power-gated, or is the spin-table entry not
  taken? Pure PMGR PS reads + ADT parse, zero risky MMIO (same safety class as `soc_recon`).
- **NEW `acio_status(buf)` (read-only)** — the gated next step's precondition: for each `acio-cpuN`
  ASC nub, read **only the rtkit management-endpoint status** (is the IOP running / halted / in
  reset?) via `m1n1.fw.asc` — **do NOT call `.boot()`/`.start()`**. This is the mailbox read
  RUN 24 skipped; it's what upgrades the ACIO theory from "plausible" to "proven" and decides
  whether RUN 26 boots the IOP. Wrap in `guarded`; if the ASC base is un-clocked, bail cleanly.
- **`main()`** — argparse with `--out` (default `/tmp/m4-recon`), `--require-build`, `--smp-start`,
  `--soc-recon`, `--smp-diag`, `--acio-status`; build-guard via `m4_common.require_build`; same
  partial-flush-after-each-section discipline. **No PCIe init, no phy_ip, no IOP boot.**

### 3. New wrapper — `Scripts/m1n1/soc_bringup.sh`

Clone of `perstn.sh` (the /dev/ttyACM0 check + `sudo -E env ... M1N1DEVICE=... python3
soc_bringup.py --out "$OUT_DIR" "$@"` + tail of `nic-runtime.txt`). Same `OUT_DIR=/tmp/m4-recon`.

### 4. RUN 25 dispatcher arm — `Scripts/m1n1/perstn-run.sh`

Add a `25)` arm (after `24)` at line 1350) that `exec`s **`soc_bringup.sh`** instead of `perstn.sh`.
Since the dispatcher tail hard-codes `exec "$SCRIPT_DIR/perstn.sh"` (line 1385), make the target
script selectable: set a `RUNNER` var (default `perstn.sh`) and have the `25)` arm set
`RUNNER=soc_bringup.sh`; change the final line to `exec "$SCRIPT_DIR/$RUNNER" "${FLAGS[@]}" "$@"`.
Add `25` to the two usage strings (lines 6, 1378). RUN 25 flags:

```
    25)
        # RUN 25: SoC-first bring-up moves to soc_bringup.py. RUN 24 found
        # 9/9 secondary cores REFUSED and the ACIO IOPs (ACIO0/1/3, owners of
        # the CIO3-PLL/phy_ip PHY) gate-OFF. This run (read-only, no reflash):
        # retry --smp-start, diagnose WHY cores refuse (--smp-diag), and read
        # the ACIO rtkit mailbox status (--acio-status) that RUN 24 skipped.
        # No IOP boot. Decides RUN 26: ACIO halted -> boot it; cluster power
        # -gated -> fix core power first.
        RUNNER=soc_bringup.sh
        FLAGS=(--smp-start --soc-recon --smp-diag --acio-status
               --require-build=rc1-60-g)
        ;;
```

## Critical files

- **NEW `Scripts/m1n1/m4_common.py`** — shared preamble + primitives extracted from `perstn.py`.
- **NEW `Scripts/m1n1/soc_bringup.py`** — SMP start + `soc_recon` (moved) + new `smp_diag` /
  `acio_status` read-only probes + `main()`.
- **NEW `Scripts/m1n1/soc_bringup.sh`** — wrapper cloned from `perstn.sh`.
- **`Scripts/m1n1/perstn.py`** — replace inline primitive defs with `from m4_common import ...`;
  remove the moved `soc_recon` + `--smp-start` block. No change to PCIe logic.
- **`Scripts/m1n1/perstn-run.sh`** — `RUNNER` indirection + `25)` arm + usage strings.
- Reference only (for RUN 26, not this run): `m1n1/proxyclient/m1n1/fw/asc/__init__.py`
  (`ASC.boot()`/`.start()` at lines 119/103 — the IOP boot API), `m4_recon/adt.txt:2639`
  (acio-cpu0), `m1n1/src/smp.c` (T8132 secondaries), `docs/plans/run_24.md`.

Follow-up still owed (unchanged): `Scripts/m1n1/logs/{18..24}/findings.md`.

## Verification

Real hardware (M4 mini + m1n1 over UART); the **user** runs the live steps. No reflash (rc1-60-g).

1. **Static (safe to run here):**
   - `python3 -c "import ast; ast.parse(open('Scripts/m1n1/m4_common.py').read())"`
   - `python3 -c "import ast; ast.parse(open('Scripts/m1n1/soc_bringup.py').read())"`
   - `python3 -c "import ast; ast.parse(open('Scripts/m1n1/perstn.py').read())"` (still parses
     after the import edit)
   - `bash -n Scripts/m1n1/soc_bringup.sh` and `bash -n Scripts/m1n1/perstn-run.sh`
   - Confirm `perstn-run.sh 25` routes to `soc_bringup.sh` and its flags parse
     (`python3 soc_bringup.py --help`), and that `perstn-run.sh 24` still routes to `perstn.sh`.
2. **Live:** `./Scripts/m1n1/perstn-run.sh 25`.
3. **Read** `/tmp/m4-recon/nic-runtime.txt` + the `TTY>` console, keying on:
   - `--smp-start`: still 9/9 refused, or did any core come up this build?
   - `--smp-diag`: for each refused core — cluster PMGR gate ON or OFF? Reset-manager held?
     (Distinguishes "cores power-gated" from "spin-table not taken".)
   - `--acio-status`: is `acio-cpu0` rtkit **running / halted / in-reset**? (The read RUN 24
     never did — this is what proves or breaks the ACIO-owns-the-PHY theory.)
   - exc_count deltas stay 0 (recon stayed wedge-free).
4. **Outcome → RUN 26 (record in `logs/25/findings.md`):**
   - ACIO halted/in-reset AND cores refuse on cluster power → **boot the ACIO IOP** and/or fix
     cluster power in RUN 26 (`ASC(u, acio_base).boot()`).
   - Cores refuse but ACIO already running → ownership theory weakens; refocus on core startup.
   - Any core comes up on rc1-60-g → SMP path works; proceed to per-IOP bring-up ordering.

## Non-goals for this run

No IOP boot, no `phy_ip`/CIO3-PLL touch, no `pcie_init` changes, no reflash. `perstn.py`'s PCIe
logic is untouched except for the mechanical import extraction.
