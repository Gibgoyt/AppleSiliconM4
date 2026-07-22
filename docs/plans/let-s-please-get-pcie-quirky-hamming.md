# RUN 11: fix the C path — port-slice filter in m1n1 pcie.c + full `p.pcie_init()`

## Context

RUN 10 falsified the tight-timing axis: the on-CPU stub (340 bytes at `0x1000cce0000`, 29-entry table, first target `0x497040038`) uploaded cleanly, `p.call` went out, and m1n1 died — `RAISED: UartTimeout` is the last log line (`logs/10/nic-runtime.txt:5775`). Everything before 6.g was byte-identical to RUN 9 modulo free-running counters.

Git archaeology then reframed the entire blocker:

- On **2026-07-11** (`d664bd9` era), `p.pcie_init()` — m1n1's full C-side init — **ran to completion on this machine** (that era's code says: *"After p.pcie_init() has returned (with ports stuck at LINKSTS_BUSY)"*). That means the C path's **per-port bring-up (pcie.c:571-852)** ran, and afterwards Phase F's 6.g applied all 29 PLL tunables through `phy_ip+0x38` via `p.tunables_apply_local` **successfully** — phy_ip decoded. The wedge that day was only at 6.h: the C applicator wrote the auspma **port-1 slice** into the unpowered slice (j773g has no pci-bridge1).
- Commit `6b277bc` then moved the phy-ip tunables into the C path itself ("fixes t8132 LTSSM stuck at BUSY"), which made `p.pcie_init()` wedge C-side on the same port-1 slice bug — so `--no-pcie-init` became the baseline and the 20+-run Python replay series began.
- The replay **never runs per-port bring-up** (it was gated behind Phase F success), and phy_ip has **never decoded in any replay boot** — from any state, via proxy or on-CPU stub, with every pcie.c pre-phy_ip write applied and all APCIE PMGR gates ON. Conclusion: the phy_ip unlock lives in the parts of `pcie_init` the replay never executes (most plausibly per-port PHY power-up ungating the shared PHY-IP block).

**Decision (user-approved): stop the register archaeology and fix the known-good C path.** Its only known-fatal bug is the port-1 slice write. Patch `pcie.c` with the same slice filter the Python side already uses, rebuild m1n1, re-enroll it, and run the full `p.pcie_init()` with post-init dumps.

## Changes

### 1. m1n1 fork: slice-filtered phy-ip tunables (`/home/ahmed/Projects/C/embedded/m1n1/src/pcie.c`)

At the phy-ip tunables application (pcie.c:518-526, the `tunables_apply_local_addr(path, pll_prop/auspma_prop, state->phy_ip_base[phy])` calls on the T8140 gate):

- Add a static helper `tunables_apply_phy_ip_filtered(...)` used **only for the t8132 case** (gate on `adt_is_compatible(..., "apcie,t8132")` or a new `reg_info` flag, so other chips keep exact current behavior). It iterates the tunables property entries itself (same entry format m1n1's `tunables_apply_local_addr` parses — offset/size/mask/value) and applies each via the existing low-level write path, **skipping entries targeting an absent port's slice**:
  - Slice geometry (mirror `pcie_regs.py:100-102` and `classify_phy_ip_offset` at 383-413): `offset < 0x8000` → shared, always apply; else `idx = (offset - 0x8000) / 0x8000`, apply only if port `idx`'s bridge exists; `offset >= 0x20000` → skip.
  - Port presence: reuse how `pcie_init_controller` already detects bridges (it iterates `pci-bridge%d` ADT subnodes for per-port init; hoist/reuse that check — implementer to locate the exact mechanism in pcie.c and reuse it rather than re-deriving).
  - Apply the filter to BOTH `apcie-phy-ip-pll-tunables` (all 29 shared today — filter is a no-op but future-proof) and `apcie-phy-ip-auspma-tunables` (146 entries, the port-1 slice is the known killer).
  - `printf` a summary (applied/skipped counts) so the UART log shows the filter working.
- Commit in the m1n1 fork with a message referencing the 2026-07-11 wedge and j773g's missing pci-bridge1.

### 2. Build + stage m1n1

- `cd /home/ahmed/Projects/C/embedded/m1n1 && cp build/m1n1.macho build/m1n1.macho.prepatch && PATH="$HOME/.cargo/bin:$PATH" make -j$(nproc)` (PLAN.md:137-139 flow).
- Stage: `mkdir -p /tmp/m4-serve && cp build/m1n1.macho /tmp/m4-serve/` (+ keep the prepatch rollback there too, per PLAN.md:264-265).
- **User step (physical):** boot the M4 into recovery/1TR, fetch the macho over HTTP, `kmutil configure-boot -c /tmp/m1n1.macho -v ...` (established flow in PLAN.md / PLAN_2.md:855-856, rollback documented).

### 3. Dispatcher: RUN `11)` arm in `Scripts/m1n1/perstn-run.sh`

Full-C-init mode — does NOT use `BASE_FLAGS` (which hard-codes `--no-pcie-init` + `--t8140-replay`; Phase F would wedge at 6.g before `pcie_init` ever ran). Flags:

```bash
FLAGS=(--preinit-probe
       --pmgr-enable
       --pmgr-per-port
       --gate-poke
       --tier3)
```

(i.e. keep the PERSTN/CLKREQ pokes + Phase 0/D gate work that the C init benefits from, omit `--no-pcie-init` so `p.pcie_init()` runs at perstn.py:5536, then the existing post-init machinery fires automatically: `dump_pcie_regs(post-init, tier=3)` at 5555-5563, LTSSM kick + post-kick dump at 5565-5588.) Comment block: the 2026-07-11 archaeology, the port-1 fix, and the interpretation matrix. Update usage strings to `…|10|11}` and append the chronicle sentence (RUN 10 falsified tight-timing; RUN 11 = patched-m1n1 full init).

Also verify (implementer): `dump_pcie_regs` tier 3 includes a **phy_ip shared-window dump** post-init — if it doesn't, add one (guarded reads of `phy_ip_base+0x0..0x8000` at stride, flushed) so the boot harvests the decoded-phy_ip state for offline diffing. This dump is the payoff even if LTSSM still misbehaves.

### 4. Findings + docs

- **`Scripts/m1n1/logs/10/findings.md`**: stub worked mechanically (upload, cache maint, call), UartTimeout on first phy_ip access → tight-timing falsified; then the archaeology section: d664bd9 evidence chain (`p.tunables_apply_local` 6.g success on 2026-07-11, "After p.pcie_init() has returned…" quote, 6b277bc/`--no-pcie-init` history), conclusion that the unlock is in the un-replayed parts of `pcie_init` (per-port bring-up prime suspect), and the CIO3PLL/rc_base recon note (cio3pllcore/pcieclkgen inferred target rc_base, never applied there — retained as the fallback hypothesis if RUN 11 surprises).
- **`docs/project-m4-pcie-bringup.md`** post-RUN-10 block: the reframing above, RUN 11 plan, and the matrix:
  - **`p.pcie_init()` returns** → the port-1 filter fixed the C wedge. Read the post-init dumps: phy_ip decoded? per-port LINKSTS (port 0 WiFi, port 2 NIC)? If the NIC trains → jackpot, next phase = NIC driver bring-up. If ports still stuck BUSY → we now have a live phy_ip dump; RUN 12 = diff harvested state vs replay state to find the unlock register, and/or targeted LTSSM work from the C-init state.
  - **`pcie_init` wedges elsewhere** → new wedge address in C = high signal; UART log's last line identifies it (add printfs if needed).
  - **`pcie_init` returns but phy_ip still unreadable post-init** → per-port-unlock hypothesis falsified; fallback = cio3pllcore/pcieclkgen → rc_base experiment (dispatcher-only, flags already exist: `--pcieclkgen-naked-apply-to=rc_base --cio3pllcore-naked-apply-to=rc_base`).

## Verification

1. m1n1 builds clean (`make` with cargo env); `git -C .../m1n1 diff` reviewed; the filter is t8132-gated only.
2. `bash -n Scripts/m1n1/perstn-run.sh`; `python3 -m py_compile Scripts/m1n1/perstn.py` (if the phy_ip post-init dump needs adding).
3. Commits: m1n1 fork (pcie.c filter); project repo (a) `logs+docs: RUN 10 findings + 2026-07-11 archaeology + RUN 11 plan (C-side port-slice filter, full pcie_init)`, (b) `perstn: add RUN 11 dispatcher (full pcie_init mode)`.
4. User: stage + `kmutil configure-boot` the patched macho from recovery (rollback macho staged alongside), boot to m1n1, `./Scripts/m1n1/perstn-run.sh 11`.
5. Watch the UART log: the new filter's applied/skipped printout, `p.pcie_init() -> <rc>`, post-init dumps (esp. phy_ip window + LINKSTS decodes), LTSSM kick results. Copy logs to `Scripts/m1n1/logs/11/`, commit `run 11 logs`.
