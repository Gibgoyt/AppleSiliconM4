# RUN 23 — force APCIE_PHY_SW ACTIVE (--gate-poke) + clean SMC/LTSSM diagnostics

## Context

m4-pcie bring-up in `AppleSiliconM4/` (Mac16,10 / J773g / t8132) to enumerate the 1GbE NIC
(`lan-1gb`, real ADT MAC `d0:11:e5:71:81:dc` — a provisioned device, not absent) on
`pci-bridge2`. `pcie_init()` returns 0; both ports reach **LINKSTS BUSY** (port0 `0x8300020c`,
port2 `0x83000204`), never UP, ECAM vacant.

Link-training runs 18-22, in order, each falsified a hypothesis:
- **18** CLKREQ# alt-func 2 · **19** REFCLK REQ→ACK handshake (succeeds) · **20** post-init
  T602X LTSSM kick · **21** refclk-first PERST# re-sequence — all: no change.
- **22** (FIRST patched m1n1, `rc1-60-g`): ran the T602X arm/`port+0x10`/LTSSM-kick
  (incl `ltssm+0x14=START`) **in-window during pcie_init**, no SError. Result:
  `rc_base+0x3c` arm read back **`0x0`**, `port+0x10=0x0`, `ltssm+0x14=0x0`, LINKSTS unchanged.
  **So "run it in-window" is falsified too** — those T602X-gated registers won't latch from the
  AP at all, in-window or out.

### The overlooked precondition (the RUN 23 lead)

`docs/ref-asahi-t8132-pcie.md:246-252` establishes a **t8132-specific PMGR difference**:
`ps_apcie_phy_sw` (gate **151**, the PHY power switch) is **NOT always-on** on t8132 (unlike
t8122/t6030 where macOS never touches it) and needs its `sys_st`+`sys_gp` parent chains up
first. **RUN 22's Phase 0 readout shows gate 151 OFF** (`actual=0x4`) and its parent
`APCIE_SYS_ST` (150) OFF (`actual=0x0`) — `logs/22/nic-runtime.txt:413,443,477`.

**`--gate-poke` (Phase D) is the step that forces gate 151 + parents ACTIVE** before pcie_init
(`perstn.py:6144`, `probe_phaseD_gate151_poke` at 1704). **RUNs 18-22 all DROPPED `--gate-poke`**
(`perstn-run.sh:1009` "gate-poke DROPPED"), on the assumption that m1n1's own
`pmgr_adt_power_enable` (pcie.c:531) is sufficient. But `ref-asahi:307` argues that walk
"probably ALSO left APCIE_SYS_ST and APCIE_PHY_SW off," and the project's own top theory
(`project-m4-pcie-bringup.md:389`) is "**per-port PHY power-up ungating the shared PHY-IP
block**." The known-good 2026-07-11 boot and RUN 11 used `--gate-poke`.

**So every link-training run may have been writing the arm/LTSSM/PHY registers with the PHY
power switch gated OFF.** That is a plausible, cheap-to-test reason `rc_base+0x3c`/`ltssm+0x14`
won't latch and the link won't train — and it was never tried in the 18-22 series.

Secondary: RUN 22's `SMC gP0d/gP1a = 0` readback was **confounded** — `endpoint_diag` reads them
in a *fresh* SMC session after `smc_power()` already `stop()`'d. The fabric IS powered (coherent
LINKSTS/rc_base reads prove it), so gP0d=0 is likely a write-triggered-key / session artifact,
not "unpowered." A same-session readback settles it for ~free.

## Goal

One **no-reflash** boot on the existing patched `rc1-60-g` build that: (1) adds `--gate-poke` so
`APCIE_PHY_SW` is ACTIVE before pcie_init runs its in-window arm + LTSSM kick — the missing
precondition; (2) cleanly verifies SMC power with a **same-session** readback; (3) decodes the
already-dumped RC **LTSSM-debug** registers as an RC-stuck-vs-endpoint-silent discriminator. The
pivotal observable: with gate 151 forced ON, do the in-window `rc_base+0x3c` arm and `ltssm+0x14`
(printed by the RUN 22 `PCIE_BC` breadcrumbs) finally read back non-zero, and does LINKSTS move?

## Approach

### 1. RUN 23 dispatcher arm — `Scripts/m1n1/perstn-run.sh` (primary change)

Add `23)` before `*)` + `23` in the usage string. Same as RUN 22 **plus `--gate-poke`**, same
`rc1-60-g` guard (build unchanged — no reflash). Do **NOT** add `--phy-ip-probe` (that would run
Phase E's phy_ip sanity, wedge-prone); Phase D alone is PMGR-only and wedge-safe.

```
    23)
        # RUN 23: force APCIE_PHY_SW (gate 151) ACTIVE before pcie_init.
        # RUN 22 ran the T602X arm/LTSSM writes in-window but they read back
        # 0 -- and Phase 0 showed gate 151 (the t8132 PHY power switch, NOT
        # always-on) + parent APCIE_SYS_ST OFF. RUNs 18-22 all dropped
        # --gate-poke (Phase D), so the in-window arm may have run with the
        # PHY switch powered off. Re-add it: Phase D forces gate 151 + parents
        # ACTIVE (PMGR-only, wedge-safe) BEFORE pcie_init, so the patched
        # build's in-window arm/kick run with the PHY domain up. No reflash --
        # reuses the rc1-60-g build. --endpoint-diag now does a same-session
        # SMC readback + LTSSM-debug decode. Phase E stays OFF (no phy_ip).
        #
        # Matrix: with gate 151 ON, arm rc_base+0x3c reads back 0x1 /
        # ltssm+0x14=0x1 / BUSY clears -> the PHY-switch power was the missing
        # precondition -> link trains -> ECAM finds the NIC. Still 0 / BUSY ->
        # gate 151 ruled out; RUN 24 = T602X pmgr posture (pmgr_adt_power_
        # disable_index path,1 in-C, reflash) or the analog-PLL axis.
        FLAGS=(--preinit-probe
               --gate-poke
               --tier3
               --clkreq-mode=periph
               --setup-refclk=both
               --endpoint-diag
               --require-build=rc1-60-g)
        ;;
```

### 2. Same-session SMC readback — `Scripts/m1n1/perstn.py :: smc_power()` (~275)

Before `smc.stop()`, add a read-back of `gP0d`/`gP1a` **in the same open session** right after
the writes, logging value + OK/MISMATCH (mirrors the loop in `endpoint_diag` but without the
confounding fresh session). This definitively settles whether the power writes stick vs. whether
gP0d is a write-triggered key. Keep the existing `endpoint_diag` fresh-session read too, so the
two can be compared side-by-side. Reuse `smc.smcep.read32`.

### 3. LTSSM-debug decode — `Scripts/m1n1/perstn.py :: endpoint_diag()` (~4778)

The RC LTSSM-debug window is `ltssm_base` (reg 7/11/15, size 0x1000), which perstn.py already
reads in Tier 3 (`+0x10/0x14/0x1c/0x20`) **without wedging** (RUN 22 confirmed). Add to
`endpoint_diag` a read + human decode of these per active port to distinguish **RC-side stuck in
Detect** (link engine never starts) vs **RC cycling Detect↔Polling** (training but endpoint not
answering). This is the RC-vs-endpoint discriminator (agent RANK 3), no new risky reads. Also fix
the `endpoint_diag` MAC lookup (RUN 22 reported "no MAC" — it searched the bridge node, but the
MAC is on the parent; look it up correctly so we log the real `d0:11:e5:71:81:dc`).

### 4. Nothing else changes

No C patch, no rebuild, no reflash. The `rc1-60-g` build already has the in-window writes; RUN 23
only changes which power posture they execute under (+gate 151 ON) and improves diagnostics.

## Critical files

- `Scripts/m1n1/perstn-run.sh` — new `23)` arm (+`--gate-poke`, `rc1-60-g` guard) + usage.
- `Scripts/m1n1/perstn.py` — same-session SMC readback in `smc_power` (~275-285); LTSSM-debug
  decode + MAC-lookup fix in `endpoint_diag` (~4778-4820). Reuse `probe_phaseD_gate151_poke`
  (1704, already wired at 6144), `_linksts_decode`, `_read32_live`, `smc.smcep.read32`.
- Reference only: `docs/ref-asahi-t8132-pcie.md:246-252,307` (gate 151 not-always-on),
  `docs/project-m4-pcie-bringup.md:389` (per-port PHY power-up theory),
  `Scripts/m1n1/logs/22/nic-runtime.txt:413,443,477` (gate 151/150 OFF proof),
  `m1n1/src/pcie.c:531` (pmgr_adt_power_enable), `pcie.c:37/42/47` (LTSSM-debug reg).

Follow-up (still owed): `Scripts/m1n1/logs/{18,19,20,21,22}/findings.md`.

## Verification

Real hardware (M4 mini + m1n1 over UART) — the user runs it. No reflash (rc1-60-g already
enrolled from RUN 22):

1. **Static**: `python3 -c "import ast; ast.parse(open('Scripts/m1n1/perstn.py').read())"`,
   `bash -n Scripts/m1n1/perstn-run.sh`, confirm `23)` routes and flags parse.
2. **Live**: `./Scripts/m1n1/perstn-run.sh 23` (guard must show `rc1-60-g` — the patched build).
3. **Read** `/tmp/m4-recon/nic-runtime.txt` + the `TTY>` console, keying on:
   - **Phase D**: did gate 151 (`APCIE_PHY_SW`) + parent 150 converge to `actual=0xf` ACTIVE?
     (`phaseD_gate151_active` true). If Phase D can't raise them, that itself is the finding.
   - **the `PCIE_BC` breadcrumbs** (now with gate 151 ON): `t8132 arm rc_base+0x3c (post=0x?)` —
     `0x1` at last? `t8132 port N LTSSM kick: ltssm+0x14=0x?` — `0x1`? These are the experiment.
   - post-init `watch_linksts`: `BUSY CLEARED` vs `still BUSY`.
   - **same-session SMC readback**: gP0d=`0x800001` / gP1a=`1` (writes stick) vs `0`
     (write-triggered key) — settles the power question.
   - **LTSSM-debug decode**: RC parked in Detect (RC-stuck) vs cycling (endpoint-silent).
   - if BUSY clears → ECAM walk prints the NIC VID:DID (class 0x02) — the goal.
4. **Outcome matrix** (record in `logs/22/findings.md`):
   - gate 151 ON + arm reads `0x1` + `ltssm+0x14=0x1` + BUSY clears → **PHY-switch power was the
     missing precondition → link trains → NIC enumerates.**
   - gate 151 ON but arm still `0x0` / BUSY persists → gate 151 ruled out → RUN 24 = T602X pmgr
     posture (`pmgr_adt_power_disable_index(path,1)` in-C before the arm; reflash), then the
     analog-PLL axis (CIO3PLL enable pair at `phy_ip+0x2a00`, write-only, high wedge risk) as
     last resort.
   - Phase D fails to raise gate 151 → the PHY switch can't be powered from the AP at all →
     redirect to how iBoot/macOS brings it up (SMC/companion-processor ownership).
   - same-session gP0d sticks + LTSSM-debug shows RC cycling → endpoint-silent axis opens.
