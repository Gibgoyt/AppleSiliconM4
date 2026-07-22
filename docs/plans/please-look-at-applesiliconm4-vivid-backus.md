# RUN 20 — PCIe LTSSM link training: replay m1n1's skipped T602X LTSSM kick

## Context

m4-pcie bring-up in `AppleSiliconM4/` (Mac16,10 / J773g / t8132), poking hardware through
m1n1 to get the NIC on `pci-bridge2` to enumerate. The phy-ip tunables axis was closed by
RUN 17 (phy_ip never decodes; `pcie_init` completes without it). RUN 18/19 pivoted to the
real blocker: ports reach **LINKSTS BUSY** (`0x8300020c` / `0x83000204`) and never train.

**RUN 19 result (decisive):** the per-port REFCLK REQ→ACK handshake now succeeds on both
ports (REFCLK0ACK + REFCLK1ACK assert on `phy_base+0x000`, ending `0x2300066f`). But LINKSTS
never moved — BUSY through pcie_init, the 5 s watch, and the LTSSM kick. RUN 19 also proved
the infrastructure works: the Tier-3 `phy_extra`/`ctrl_lo` gate held (no wedge), and for the
first time the run reached the **LTSSM kick, post-kick dump, and ECAM walk** (both slots
vacant `0xffffffff` — expected, link not trained).

### Root cause (from m1n1 source, authoritative)

Reading `/home/ahmed/Projects/C/embedded/m1n1/src/pcie.c` (the actual enrolled build):

- Port comes up: `set32(+0x82c, RESET_DIS=BIT(0))` releases reset (845), STATUS_RUN poll
  passes (851; our STATUS=`0x5`). "Port failed to come up" is NOT printed. Good.
- **The LTSSM-start writes are gated `type==APCIE_T602X && controller!=APCIE` (857-864) —
  SKIPPED on our path** (t8132 uses the `APCIE_T8140` type; the main NIC ports are
  `controller==APCIE`). So `ltssm_base+0x10/0x1c/0x20/0x14` and the APPCLK bit-8 clear never
  run → LTSSM engine never told to advance → the RUN 19 dump shows `ltssm_base` all-zero.
- LINKSTS-idle poll (866) times out → prints **"Port failed to become idle"** → **`continue`
  (869)** bails out of the rest of per-port bring-up.
- The "Do it again?" retry that DOES kick LTSSM (873-889: T602X_RESET cycle + the same
  ltssm_base writes) is gated `type==APCIE_T602X` → **never runs for T8140**, and is
  unreachable anyway after the `continue`.

**Conclusion:** on t8132 (T8140 type, APCIE controller) m1n1 has **no LTSSM-start sequence at
all**. The link sits BUSY because nothing kicks the LTSSM state machine. Note: upstream
Linux's `PORT_LTSSMCTL (0x080) START` is a *different* mechanism m1n1 doesn't use — m1n1
kicks via `ltssm_base` writes, so RUN 20 replays the m1n1 sequence, not the Linux one.

### Why this is now safe to write

Every prior run feared `ltssm_base` writes would AXI-stall (it's Tier 3). RUN 19 **read
`ltssm_base+0x10/0x14/0x1c/0x20` cleanly (all `0x00000000`, no wedge)** on both ports — the
window is clocked and reachable. Writing it is now low-risk.

## Goal

Replay m1n1's exact T602X `controller==APCIE` port-completion path — the block the T8140 code
skips — after the proven refclk handshake, then watch LINKSTS for training. All the machinery
already exists in `perstn.py`; RUN 20 is primarily a **new dispatcher flag combination** plus
a small ordering fix so the LTSSM kick runs *after* refclk.

## Approach

### 1. New RUN 20 dispatcher arm — `Scripts/m1n1/perstn-run.sh`

Add a `20)` case before `*)` (~line 1231, after the RUN 19 arm) and add `20` to the usage
string. Reuse existing flags — no perstn.py logic change needed for the core experiment:

```
    20)
        # RUN 20: link training, part 3. RUN 19 proved the refclk REQ->ACK
        # handshake succeeds but LINKSTS stays BUSY. m1n1 src/pcie.c:857-889
        # shows WHY: the LTSSM kick (ltssm_base+0x10/0x1c/0x20/0x14) and the
        # "do it again" retry are BOTH gated to APCIE_T602X and skipped on the
        # T8140/APCIE path we run -- so LTSSM is never started and pcie_init
        # bails at the "failed to become idle" poll. ltssm_base read cleanly
        # (all-zero, no wedge) in RUN 19, so writing it is safe now.
        #
        # Replay m1n1's T602X controller==APCIE completion path via
        # --t602x-init --t602x-aggressive --t602x-do-again (t602x_port_init_
        # replay: rc_base+0x3c gate, port writes, T602X_RESET cycle, the
        # ltssm_base kick writes, rc_base+0x3c clear-to-arm), on top of the
        # RUN 19 refclk handshake. Keep the ADT pin mux; phy_extra probes
        # stay gated. The post-t602x dump + ECAM walk report the result.
        #
        # Matrix: BUSY clears -> LINK TRAINS -> ECAM walk finds the NIC ->
        # BAR setup + driver. Still BUSY -> the ltssm_base writes weren't the
        # trigger (or a further gate remains); RUN 21 = explicit PORT_LTSSMCTL
        # 0x080 START (Linux mechanism) / PERST re-toggle / endpoint-presence
        # diagnostics.
        FLAGS=(--preinit-probe
               --tier3
               --clkreq-mode=periph
               --setup-refclk=both
               --t602x-init
               --t602x-aggressive
               --t602x-do-again
               --require-build=rc1-59-g)
        ;;
```

### 2. Ordering fix — run refclk BEFORE the T602X replay — `Scripts/m1n1/perstn.py`

Current main() post-init order (`~perstn.py:6069+`): early tier-1 dump → **setup_refclk(post)**
→ LINKSTS watch → post-init dump → LTSSM kick → **T602X replay** (`--t602x-init`, ~6198) →
post-t602x dump. That order is already correct: refclk(post) runs before the T602X replay.
No change strictly required.

One improvement: the RUN 18 **5 s LINKSTS watch currently sits before the T602X replay**, so
it reports "still BUSY" *before* the kick that might fix it. Add a **second short LINKSTS
watch (≤5 s) immediately after the T602X replay's post-dump**, so RUN 20 shows whether BUSY
clears post-kick without needing a RUN 21 just to observe it. Implement by extracting the
existing watch loop (`perstn.py:6087-6114`) into a small helper `def watch_linksts(apcie, buf,
secs=5.0)` and calling it in both places (after `setup_refclk(post)` as today, and after the
`flush("dump-post-t602x")` at ~6220). Reuse `_read32_live`/`_linksts_decode`/`p.read32`.

### 3. Nothing else changes

`setup_refclk`, `t602x_port_init_replay` (incl. `aggressive`/`do_again`), the Tier-3 gate, and
the ECAM walk all already exist and are exercised by the flags above.

## Critical files

- `Scripts/m1n1/perstn-run.sh` — new `20)` arm (~1231) + usage string.
- `Scripts/m1n1/perstn.py` — extract `watch_linksts()` helper from the inline loop
  (~6087-6114); call it a second time after the post-t602x dump (~6220).
- Reference only (no edits): `/home/ahmed/Projects/C/embedded/m1n1/src/pcie.c:836-889`
  (the gated LTSSM logic), `docs/ref-asahi-t8132-pcie.md`, `Scripts/m1n1/logs/*/findings.md`.

Follow-up (not this change): write `Scripts/m1n1/logs/19/findings.md` (RUN 19 result: refclk
handshake succeeds, LTSSM never kicked on T8140 path, infra reaching ECAM) + the RUN 20 plan,
matching the per-run log convention. `logs/18/findings.md` is also still owed.

## Verification

Real hardware (M4 mini + m1n1 over UART) — the user runs it:

1. **Static**: `python3 -c "import ast; ast.parse(open('Scripts/m1n1/perstn.py').read())"`,
   `bash -n Scripts/m1n1/perstn-run.sh`, and confirm the `20)` arm routes.
2. **Live**: `./Scripts/m1n1/perstn-run.sh 20`.
3. **Read** `/tmp/m4-recon/nic-runtime.txt`, keying on:
   - the T602X replay's `snap(...)` lines — does LINKSTS change after the `ltssm_base`
     kick / T602X_RESET cycle?
   - the new post-t602x LINKSTS watch — `BUSY CLEARED after …s!` vs `still BUSY after 5 s`.
   - if BUSY clears → the ECAM walk should print the NIC VID:DID (class 0x02 network).
4. **Outcome matrix** (record in `logs/19/findings.md`):
   - BUSY clears → link trains → NIC enumerates → next phase = BAR setup / driver.
   - BUSY persists but `ltssm_base` readback now non-zero → LTSSM advanced but no link
     partner response; RUN 21 = PERST re-toggle / longer settle / endpoint-power check.
   - BUSY persists and `ltssm_base` writes read back 0 (dropped) → the block needs the
     APPCLK bit-8 clear or config-write-enable first; RUN 21 = add explicit PORT_LTSSMCTL
     (0x080) START and/or reorder.
   - ltssm_base write wedges m1n1 → revert to non-aggressive; the window regressed vs RUN 19.
