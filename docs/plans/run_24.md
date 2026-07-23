# RUN 24 — pivot: bring up the SoC (cores + companion IOPs) before PCIe

## Context

After **six** falsified PCIe-link-training runs (18–23), the user (correctly) called for a
strategic pivot: stop poking individual PCIe registers from a single core, and bring the SoC up
first. This plan follows that pivot. Two things forced it:

**1. Two long-standing "blockers" were misreads (verified against `m1n1/src/pcie.c`).**
- `rc_base+0x3c` reading back `0` is a **write-1 strobe / auto-clear**, not a locked register:
  m1n1's own working driver `set32`s (762) and `clear32`s (695/1018) it *blindly and never
  reads it back*. Only our RUN-22 diagnostic read it.
- `ltssm+0x14` (START) is written **only for `controller != APCIE`** (`pcie.c:883`). t8132 **is**
  the APCIE controller, which is meant to train **automatically** after reset-deassert. We were
  writing a register the hardware doesn't expect the AP to touch. The `ltssm+0x10/0x1c/0x20`
  values in every dump are **our own written config**, not live LTSSM state.
So RUNs 20–23's "the writes don't latch" evidence largely evaporates.

**2. We have been running every PCIe attempt on a SINGLE core, with all companion processors
asleep.** `smp_start_secondaries()` is **never called** in the perstn flow (verified: absent
from `main.c` and the perstn scripts; the boot log shows one "M4 Donan P core"). The proxy op
`p.smp_start_secondaries()` (P_SMP_START_SECONDARIES = 0x500) exists and is never used.

### The unifying new theory (evidence-backed)

The genuine, recurring blocker is the **phy_ip / analog-PLL window (`0x497040000`) that
AXI-stalls on first touch — 37 boots, closed axis (RUN 17)**. The project's own notes name that
block **"CIO3 PLL"** (`pcie_regs.py:25/31`, `perstn.py:799`, `apcie-cio3pllcore-tunables`).

**"CIO3" = the ACIO (Apple CIO / Thunderbolt) fabric.** The t8132 ADT declares an
**`acio-cpu0` companion processor** (`m4_recon/adt.txt:2639`): `compatible = iop,mxwrap-acio`,
`role = ACIO0`, an `iop-acio0-nub` of `iop-nub,rtbuddy-v2`, its own MMIO/interrupts, and a
`function-reconfig_req_interrupt`. On Apple silicon the **ACIO rtkit IOP owns the CIO/PCIe PHY
fabric** — and m1n1 **never boots it**. This coherently explains why the CIO3-PLL/phy_ip window
never comes alive no matter what the AP writes: **its owner (the ACIO IOP) was never started.**
This is exactly the "SMC/companion-processor ownership hunt" the project flagged
(`project-m4-pcie-bringup.md:382`) but never executed — and it matches the user's instinct that
the whole SoC must come up first.

## Goal

One **read-only-first, no-reflash** boot that (a) starts the secondary CPU cores, (b) does a
safe recon of the companion IOPs (ACIO especially) and which apcie-related power/clock domains
are still asleep, and (c) reads the **live** LTSSM-debug state we've never actually looked at —
to decide whether the next step is "boot the ACIO IOP" (owner of the PHY PLL) vs an
endpoint-side issue. **No IOP boot or phy_ip touch in this run** — those are the higher-risk
RUN 25, gated on what this recon finds.

## Approach

### 1. Start secondary cores — `Scripts/m1n1/perstn.py` (early in main, before pcie work)

Call `p.smp_start_secondaries()` once near the start of the run (after the build-guard check,
before `smc_power`), wrapped in `try_`/guarded, logging the console output (m1n1 prints
"Starting CPU N (die:cluster:core)..." per core). This is the standard m1n1 proxy op with an
explicit `T8132` case (`smp.c:289`) — low risk. Gate behind a new `--smp-start` flag so it's
opt-in. Record how many cores came up (and any that refused — directly tests the user's
"M4 cores physically separate / not all cores start" hypothesis).

### 2. Companion-IOP + asleep-domain recon (read-only) — `Scripts/m1n1/perstn.py`

New `def soc_recon(buf)` (near `endpoint_diag`, reuse `u.adt`, `_read32_live`, PMGR helpers),
gated behind `--soc-recon`. All ADT-parse + safe PMGR PS reads, **zero risky MMIO**:
- **Enumerate the IOPs**: walk `/arm-io` for `compatible` containing `iop,` / `ascwrap` /
  `rtbuddy` / `mxwrap-acio` (acio-cpu0/1/3, aop, pmp, dcp, sio…). For each: log role, reg base,
  `clock-gates`, and its PMGR gate state (ACTIVE vs OFF) via the existing `_load_pmgr_devices` /
  `_read_pmgr_gate_state` path (`perstn.py` Phase-0 machinery). **Which IOPs are powered but not
  booted?** ACIO0's gate is 338 (`adt.txt:2645`).
- **Map apcie's power/clock dependency on ACIO**: compare apcie `power-gates`
  `[379,380,381,382,383,151]` and the CIO3-PLL block against the acio clock/power domains — is
  the phy_ip window inside or downstream of an ACIO-owned domain?
- **Dump the ACIO mailbox/rtkit state** read-only (is the IOP running, halted, or in reset?) —
  reuse the ASC management-endpoint *status* read only; do NOT boot it here.

### 3. Live LTSSM-debug harvest (the never-read instrument) — `Scripts/m1n1/perstn.py`

Extend `endpoint_diag` (or a small helper): sweep the `ltssm_base` LTSSM-debug window
(size 0x1000) at a range of offsets (e.g. `0x0..0x100` step 4) — **not** just the 4 config
offsets we wrote. This window is proven Tier-1 safe (RUN 23 read it, exc_count 0). Decode the
`LINKSTS` top field (`0x83000000`) and the port0-vs-port2 **bit3 delta** (port0 set, port2
clear — the one real endpoint-substate signal). Verdict: **RC parked-in-Detect** (link engine
never started → points at the PHY PLL / ACIO) vs **RC cycling** (training but endpoint silent).

### 4. New RUN 24 dispatcher arm — `Scripts/m1n1/perstn-run.sh`

`24)` + usage. No `pcie_init` link-training changes; this run is recon. Keep the `rc1-60-g`
guard (same build; no reflash). Do **not** add `--gate-poke`/phy_ip/`--tier3-phyextra`.

```
    24)
        # RUN 24: SoC-first pivot. 6 falsified PCIe runs; two "blockers"
        # (rc_base+0x3c, ltssm+0x14) were write-strobe/wrong-controller
        # misreads. Every attempt ran on ONE core with the ACIO companion
        # IOP (iop,mxwrap-acio, role ACIO0 -- owner of the CIO/PCIe PHY, the
        # "CIO3 PLL" block that AXI-stalls) never booted. This run: start
        # secondary cores (--smp-start), read-only recon of the IOPs +
        # asleep apcie/CIO power domains (--soc-recon), and harvest the LIVE
        # LTSSM-debug state (never actually read -- prior runs read our own
        # written values). No IOP boot, no phy_ip, no reflash. Decides RUN 25:
        # boot the ACIO IOP (PHY owner) vs endpoint-side.
        FLAGS=(--require-build=rc1-60-g
               --smp-start
               --soc-recon
               --endpoint-diag
               --no-pcie-init)
        ;;
```

(`--no-pcie-init` so this is pure recon — but still resolve the ApcieMap so `endpoint_diag`'s
LTSSM read has the port bases. If the LTSSM window needs pcie_init to have run to be readable,
drop `--no-pcie-init`; verify from RUN 23 whether the ltssm read at exc_count-0 required init —
it ran post-init, so keep `--no-pcie-init` only if a pre-init ltssm read is confirmed safe,
else run pcie_init and just skip the link-training writes.)

## Critical files

- `Scripts/m1n1/perstn.py` — `p.smp_start_secondaries()` call + `--smp-start` (~main start,
  after guard ~5947); new `soc_recon()` + `--soc-recon` (near `endpoint_diag` ~4791); LTSSM
  window sweep in `endpoint_diag`. Reuse `_load_pmgr_devices`/`_read_pmgr_gate_state` (Phase-0),
  `_read32_live`, `_linksts_decode`, `SMCClient`/`ASC` pattern (`fw/asc`).
- `Scripts/m1n1/perstn-run.sh` — new `24)` arm + usage.
- Reference only: `m1n1/src/smp.c:237-395` (secondaries, T8132 case), `m1n1/proxyclient/m1n1/
  fw/asc/__init__.py` (IOP boot API for RUN 25), `m4_recon/adt.txt:2639` (acio-cpu0),
  `m1n1/src/pcie.c:762/883` (strobe + APCIE auto-train), `docs/project-m4-pcie-bringup.md:382`.

Follow-up (still owed): `Scripts/m1n1/logs/{18..23}/findings.md`.

## Verification

Real hardware (M4 mini + m1n1 over UART) — the user runs it. No reflash (rc1-60-g enrolled):

1. **Static**: `python3 -c "import ast; ast.parse(open('Scripts/m1n1/perstn.py').read())"`,
   `bash -n Scripts/m1n1/perstn-run.sh`, confirm `24)` routes + flags parse.
2. **Live**: `./Scripts/m1n1/perstn-run.sh 24`.
3. **Read** `/tmp/m4-recon/nic-runtime.txt` + the `TTY>` console, keying on:
   - **`--smp-start`**: how many cores start ("Starting CPU N ...")? Any refuse? (Tests the
     "M4 cores don't all come up" hypothesis directly.)
   - **`--soc-recon`**: is `acio-cpu0` (and aop/pmp) powered but un-booted? Is the CIO3-PLL /
     phy_ip window inside an ACIO-owned domain? Which apcie gates are ACTIVE vs OFF?
   - **LTSSM-debug harvest**: RC parked-in-Detect vs cycling; the LINKSTS top field / bit3 delta.
4. **Outcome matrix** (record in `logs/23/findings.md`):
   - ACIO IOP is powered-but-halted AND RC parked-in-Detect → **strong: the PHY PLL is owned by
     the un-booted ACIO IOP** → RUN 25 = boot the ACIO rtkit IOP (`ASC(u, acio_base).start()`),
     then retry pcie_init.
   - Secondary cores refuse to start → the SoC-bring-up problem is real and upstream of PCIe →
     RUN 25 = fix core startup first.
   - RC cycling (not Detect) → endpoint-side → RUN 25 = properly-ordered endpoint reset.
   - Nothing asleep / all IOPs already up → the ownership theory weakens → revisit the
     analog-PLL write-only axis, or accept the AP-side wall and write up the honest state.
