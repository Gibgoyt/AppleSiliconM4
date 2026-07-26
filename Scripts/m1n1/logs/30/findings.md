# RUN 30 findings — 0x88000 eliminated; the M4 CPU-start register is bootloader-private

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 30` → `soc_bringup.sh` on m1n1 `v1.6.0-rc1-60-g`.
Flags: `--smp-start --smp-probe --cpustart-decode --cpustart-offset=0x88000`. The run **completed
cleanly** (`[flush:done]`, m1n1 alive, exc_count delta 0 — no SError).

## 0x88000 eliminated (clean read, all-zero, no cpu6 match)

`--cpustart-decode` probed the T6040 candidate offset `pmgr+0x88000` (`0x380788000`). Result:
**every word `+0x0..+0x3c` read `0x00000000`.** The running core cpu6's bit (expected flat `0x40` or
per-cluster `0x10`) is absent. So `0x88000` is **mapped but inert/empty** on t8132 — not the
CPU-start register. (It's the T6031/T6040 offset, for bigger dies; empty on the smaller base-M4 die.)

State so far: `0x34000` reads a persistent non-zero `0x300` but doesn't reflect core state (inert);
`0x88000` reads all-zero. Neither is the register.

## Research verdict: the register is bootloader-private (not OS-visible)

- **Linux t8132.dtsi uses `enable-method = "spin-table"`, `cpu-release-addr = <0 0>`** for every
  CPU. Linux does **not** touch a pmgr CPU-start register — the cores are released by the
  **bootloader** (iBoot/m1n1), and Linux only writes the spin-table release address afterward.
- **`t8132-pmgr.dtsi` declares only power-state gates** (`ps_*` at 0x108, 0x110, …) — **no cpu-start
  reg**. The pmgr node is at `0x380700000` (matches). The CPU-start block is a raw MMIO region
  m1n1's hardcoded `CPU_START_OFF` table RE'd per-SoC; **t8132's `0x34000` was never RE'd** (bare
  `case T8132:` guess, commit `0a9302e`).
- **Not the AIC** (AIC v3 on t8132 handles IPIs, not CPU start). **`reg-private` (0x21x0100000) is
  CPU-local space** — the same family as the acc-impl window that SError'd in RUN 28; not safely
  AP-readable and not the start register.
- The remaining offset guesses (`0x30000`, `0x38000`, `0x54000`, `0x28000`) are other SoCs' values
  with **no t8132 basis**.

Baseline re-confirmed: 9/9 secondaries `Failed!`; cores powered; RVBAR correct
(→ `0x10002b84000` = m1n1 `_vectors_start`) but `LOCK=True`.

## RUN 31 plan (read-only, no reflash) — the LAST offset search

`./Scripts/m1n1/perstn-run.sh 31` — `--cpustart-scan`: a **curated bounded scan** of the pmgr window
(the known SoC `CPU_START_OFF` banks + a `0x34000` neighborhood sweep) for the word that reflects the
running core (== flat `0x40` / per-cluster `0x10`, or a plausible single-core mask). It reads the
first word of each region first and **aborts the whole scan on the first SError** (a wrong offset
desyncs the proxy — **power-cycle the M4 first and between runs**).

**Outcome fork (pre-agreed with user):**
- **A candidate word reflecting cpu6 found** → real offset located → RUN 32 = reflash smp.c with
  `CPU_START_OFF_T8132 = <that offset>` + a dedicated `case T8132:`; success = a secondary prints
  `OK`/`RVBAR entry`/`Started.`
- **No candidate anywhere (or SError abort)** → the M4 release is **not an AP-visible pmgr strobe**
  (bootloader-private, likely iBoot/SMC-internal). **STOP the offset hunt** — reassess whether SMP is
  required for the original PCIe goal, or escalate to Asahi/m1n1 developers with the full RUN 24–31
  evidence (cores powered, RVBAR correct-but-locked, 0x34000 inert, 0x88000 empty, spin-table /
  no-OS-declaration). No m1n1 branch boots M4 secondaries; this is genuinely-unsolved territory.
