# RUN 32 — Full PCIe bring-up + descend behind the bridge to identify the NIC

## Context — why this change

PLAN_3.md (`AppleSiliconM4/PLAN_3.md`) declares the PCIe wall down: on the rebased
m1n1 (`v1.6.0-42-gdf2ae61`), `p.pcie_init()` returns 0, both ports leave
`LINKSTS BUSY`, and the two Apple root bridges answer in ECAM
(`106b:100c class=06.04.00`). RUN 32 is the **pivot back to the goal** after runs
28–31 chased SMP into a dead end (SMP is parked — a single-core TCP server needs
exactly one core, which we have).

The verified gap is concrete. In `logs/20/nic-runtime.txt`:

```
p.pcie_init() -> 0
port0/port2: BUSY CLEARED after 0.00s!
00:00.0: VID:DID = 106b:100c  class=06.04.00  bridge: primary=0 secondary=0 subordinate=0
00:02.0: VID:DID = 106b:100c  class=06.04.00  bridge: primary=0 secondary=0 subordinate=0
=== enable NIC ===  (No class-0x02 device found)
```

The bridges are up but their **bus numbers were never programmed**
(`secondary=0`), so `ecam_walk` (`perstn.py:425-428`) hits `sec == 0` and
`continue`s — it never descends to bus 1 where the NIC (`pci-bridge2/lan-1gb`,
MAC `d0:11:e5:71:81:dc`) lives. Because the NIC has **no VID:DID in the ADT**, we
cannot identify the chip (and cannot write Phase-5 driver) until we read its PCI
config space. That read is standard PCIe enumeration, gated only on programming
`primary/secondary/subordinate` at bridge config `+0x18`.

**Goal of RUN 32 (Milestone M3.5):** run the *full, proven* PCIe-up sequence
first to confirm link + bridges reliably come up, then program bridge bus
numbers, re-walk bus 1, and print a real `VID:DID class=02.xx.xx` + BAR0 for the
NIC.

User decisions folded in:
1. **Bring PCIe fully up with the complete sequence first** — reuse perstn.py's
   entire phase chain (SMC → GPIO CLKREQ/PERSTN → `pcie_init` → refclk/perst →
   LINKSTS watch), not a stripped-down `pcie_init`-only path. Make the link solid
   before touching enumeration.
2. **Extract shared functions into a new, appropriately-named shared module** —
   do not `import from perstn`. perstn.py and the new `nic_bringup.py` both import
   the shared module.

---

## Deliverables

Three files under `AppleSiliconM4/Scripts/m1n1/`:

1. **`pcie_common.py`** (new shared module) — the PCIe/ECAM primitives extracted
   from `perstn.py`, so both scripts share one copy (mirrors how `m4_common.py`
   already holds the connection + liveness primitives).
2. **`nic_bringup.py`** (new) — full PCIe bring-up, then bridge bus-number
   programming + descend + NIC identify. The Phase-3.5 script.
3. **`nic_bringup.sh`** (new) — wrapper, a near-verbatim copy of `soc_bringup.sh`
   /`perstn.sh` (device auto-detect, `sudo -E`, `--out /tmp/m4-recon`).

Plus one edit:

4. **`perstn-run.sh`** — add the `32)` case arm (RUNNER=`nic_bringup.sh`).

No m1n1 reflash, no kernel changes. Every new MMIO read/write stays `guarded()`
+ `check_alive()` so a partial link leaves a log, not a wedge.

---

## 1. `pcie_common.py` — extract shared PCIe primitives

Move (cut from `perstn.py`, paste here) the following, and have both files import
them from `pcie_common`. All are pure helpers with no perstn-specific state:

- **Config-space accessors** (`perstn.py:104-121`): `ecam_addr`, `cfg_read32`,
  `cfg_read16`, `cfg_read8`, `cfg_write16`. **Add** the missing `cfg_write8`
  (needed to program single bus-number bytes at +0x18/0x19/0x1a) and
  `cfg_write32`.
- **PCI constants** (`perstn.py:80-96`): `PCI_VENDOR_ID … PCI_SUBORDINATE`,
  `PCI_CMD_*`. (Already includes `PCI_PRIMARY_BUS=0x18`, `PCI_SECONDARY_BUS=0x19`,
  `PCI_SUBORDINATE=0x1a`.)
- **Enumeration** (`perstn.py:298-476`): `_describe_class`, `probe_device`,
  `ecam_walk`, `enable_nic`.
- **PCIe bring-up phase functions**: `smc_power` (`:126`), the GPIO helpers
  `gpio_read/gpio_set_output/deassert_perstn/gpio_set_periph/assert_clkreq`
  (`:204-296`) and their pin constants (`PERSTN_PIN`, `CLKREQ_PIN`, the GPIO
  register-layout constants at `:167-201`), plus the post-init helpers
  `_linksts_decode`/`watch_linksts` (`:4285-4342`), `perst_resequence`
  (`:4344`), `setup_refclk` (`:4581`).

These already import `u`, `p`, `guarded`, `check_alive`, `_read32_live`, `try_`,
`log` from `m4_common` — keep that. `ApcieMap` stays in `pcie_regs.py`; import it
where needed.

**Refactor discipline (keep perstn.py green):** in `perstn.py`, replace the moved
`def`s with `from pcie_common import (...)`. perstn.py currently *calls* these by
bare name throughout `main()` — a single import binding preserves every call
site, so runs 1–31 behave identically. Grep `perstn.py` for each moved symbol and
confirm no residual `def`. `probe_device`/`ecam_walk` reference the PCI_*
constants and `_describe_class` — move the whole cluster together so there are no
dangling names.

## 2. `nic_bringup.py` — full bring-up + descend + identify

Structure mirrors `soc_bringup.py` (argparse, `--out`, `require_build`,
`StringIO` buf flushed per-section to `nic-runtime.txt`). Import bring-up +
ECAM helpers from `pcie_common`, connection/liveness from `m4_common`,
`ApcieMap` from `pcie_regs`.

**Phase A — full PCIe bring-up (make link solid FIRST).** Run the exact proven
sequence perstn.py's `main()` runs, each wrapped in `try_()`/`guarded()`:
1. `require_build("v1.6.0-42-g")` guard, then `apcie = ApcieMap.from_adt(u)`.
2. `smc_power(buf)` — power the apcie fabric.
3. Resolve PERSTN/CLKREQ pins from `apcie` NIC port (fallback `PERSTN_PIN`/
   `CLKREQ_PIN`); `gpio_set_periph(clkreq_pin, 2, buf)` (ADT-declared periph mux)
   + `deassert_perstn(buf, pin=perstn_pin)`.
4. `p.pcie_init()` (the upstream BIT(4) path); log rc.
5. `setup_refclk(apcie, buf)` + `watch_linksts(apcie, buf, label="post-init")` —
   **gate on BUSY clearing** on the NIC's port (port 2). Log the LINKSTS decode.
6. If BUSY does *not* clear: run PLAN_3 §3.5.2 fallback `perst_resequence(apcie,
   buf, perstn_pin)` then re-watch. This is the "make PCIe work first" insurance.

**Phase B — program bridge bus numbers (the NEW step perstn.py lacks).** For each
active root port bridge found by an initial `ecam_walk` (expect `00:00.0` and
`00:02.0`, `106b:100c`):
1. Enable the bridge command reg: `cfg_write16(base, b, d, f, PCI_COMMAND,
   cmd | PCI_CMD_MEM | PCI_CMD_BM)` (0x6).
2. Program bus numbers via `cfg_write8`: `primary=0`,
   `secondary = <bridge index + 1>` (port 0 → bus 1, port 2 → bus 2 so the two
   windows don't collide), `subordinate=0xff` (open wide; tighten later).
   Read back all three and log `old → new`.

**Phase C — re-walk + identify.** Re-run `ecam_walk(base, buf,
active_ports=apcie.active_ports)`. With `secondary != 0` the existing walk
descends automatically (the `sec == 0: continue` guard at `perstn.py:425-428` no
longer trips) and probes `<sec>:00.0`. For any device found:
- log `VID:DID`, `class`, header type (already done by `probe_device`);
- call `enable_nic(base, nic, buf)` on the class-0x02 device — it sets MEM+BM and
  sizes/reads BAR0 (`perstn.py:443-476` — already does exactly this).

**Phase D — summary.** Write an `M3.5` block to `nic-runtime.txt`: the NIC's
`VID:DID`, class, BAR0 base, and a one-line verdict (found / not-found →
which §3.5.2 fallback to try next). Print the same to stdout.

**Guardrails:** every Phase-B/C config write is `guarded()` + a `check_alive()`
gate between phases; a wedge flushes the partial log and stops (never a silent
SLVERR reboot). `dart-apcie2`/`ctrl_lo` overlap is **not** touched (that's Phase 4).

## 3. `nic_bringup.sh` — wrapper

Copy `soc_bringup.sh` verbatim, changing only the header comment and the python
target to `nic_bringup.py`. Keeps `find_m1n1_dev`, `M1N1DEVICE` override,
`sudo -E env PATH=$PATH`, `--out "$OUT_DIR"` (`/tmp/m4-recon`), and the trailing
`cat nic-runtime.txt`.

## 4. `perstn-run.sh` — add the `32)` arm

Insert before `*)` (after the `31)` arm at `perstn-run.sh:1556`), matching the
soc-run style:

```bash
    32)
        # RUN 32: PIVOT BACK TO THE GOAL. Runs 28-31 dead-ended on SMP; SMP is
        # parked (single-core TCP server needs one core). This run does PLAN_3
        # Phase 3.5: full PCIe bring-up (SMC -> CLKREQ/PERSTN -> pcie_init ->
        # refclk/perst -> LINKSTS BUSY-clear) to prove the link is solid, THEN
        # programs bridge bus numbers (primary/secondary/subordinate at cfg+0x18
        # -- the step ecam_walk never did, leaving secondary=0) and descends to
        # bus 1 behind pci-bridge2 to read the NIC's VID:DID + class + BAR0.
        # Outcome (Milestone M3.5): a real VID:DID decides Phase 5 (driver) or
        # the R2/USB-CDC-ECM fork. Commit the VID:DID to m4_recon/recon-summary.md.
        RUNNER=nic_bringup.sh
        FLAGS=(--require-build=v1.6.0-42-g)
        ;;
```

Also add `32` to the two usage strings (`perstn-run.sh:1558` and file header).

---

## Files to modify / create

- **create** `AppleSiliconM4/Scripts/m1n1/pcie_common.py`
- **create** `AppleSiliconM4/Scripts/m1n1/nic_bringup.py`
- **create** `AppleSiliconM4/Scripts/m1n1/nic_bringup.sh` (chmod +x)
- **edit** `AppleSiliconM4/Scripts/m1n1/perstn.py` — replace moved `def`s with
  `from pcie_common import (...)`
- **edit** `AppleSiliconM4/Scripts/m1n1/perstn-run.sh` — add `32)` arm + usage

## Reused, not rewritten

- `m4_common.py`: `u, p, GUARD, log, try_, flush_partial_log, require_build,
  guarded, check_alive, _read32_live, GUARD_SENTINEL` — connection + liveness.
- `pcie_regs.py`: `ApcieMap.from_adt(u)` → `.ecam_base` (0x1cb0000000),
  `.active_ports` ([0, 2]), NIC port GPIO pins.
- All ECAM/bring-up logic comes from perstn.py **via the extracted module** — the
  only genuinely new code is Phase B (bus-number programming) + the Phase-D
  summary. `enable_nic` already does BAR0 sizing/readback.

## Verification (end-to-end on hardware — user runs privileged commands)

The user drives the M4 (booted into m1n1). I print exact commands and read logs
back — no privileged runs execute here.

1. **Regression:** confirm the extraction didn't break perstn.py:
   `python3 -c "import perstn"` (import-only; must not error) and a quick
   `./Scripts/m1n1/perstn-run.sh 20` — the log must still show
   `pcie_init() -> 0` and both bridges `106b:100c`, byte-comparable to
   `logs/20/nic-runtime.txt`.
2. **RUN 32:** power-cycle the M4 into m1n1, then
   `./Scripts/m1n1/perstn-run.sh 32`.
3. **Pass (M3.5):** `/tmp/m4-recon/nic-runtime.txt` shows the full bring-up
   (`pcie_init() -> 0`, both ports `BUSY CLEARED`), then after bus-number
   programming a `--- downstream bus 2 ---` section with a device whose
   `VID:DID` is real (not `ffff:ffff`), `class=02.xx.xx`, and a plausible
   non-`0xffffffff` BAR0. `enable_nic` prints CMD `0x0 → 0x6` and a BAR0 window.
4. **On success:** record the VID:DID in `AppleSiliconM4/m4_recon/recon-summary.md`
   (closes PLAN_3 Q2/Q3), which selects the Phase-5 branch (5A discrete driver vs
   5B Apple-silicon / USB-CDC-ECM fork).
5. **On not-found:** the summary names which §3.5.2 fallback ran / to run next
   (link not fully trained → poll `LINKSTS bit0` + re-`perst_resequence`; phy_ip
   re-test from post-BIT(4) state; PERST#/refclk timing). The guarded log
   pinpoints where it stopped — no wedge.
