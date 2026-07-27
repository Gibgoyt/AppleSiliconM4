# PLAN_3 — From "PCIe link is up" to a working bare-metal TCP server on M4

> Supersedes the stalled portions of PLAN.md/PLAN_2.md. Those two remain the
> spec for the *end goal* (§0 rules, §6 payload model, §7–§13 phase mechanics,
> §14 risks). PLAN_3 is the **current, evidence-grounded execution path** given
> the 2026-07-27 breakthrough.

---

## Context — why this plan exists

For ~31 runs the project was wedged on two walls, both of which turned out to be
**already solved upstream in AsahiLinux/m1n1**. We rebased the m1n1 fork onto
upstream `main` (branch `t8132-rebase`, banner `v1.6.0-42-gdf2ae61`), keeping only
two local commits (`TARGET=T8132`, the dockchannel bounded-spin diagnostic), and
re-enrolled it via 1TR. Re-running the old numbered runs on the new build gave a
**decisive, verified result**:

- **PCIe: BREAKTHROUGH.** The upstream `APCIE_PHY_CTRL_RESET` BIT(7)→BIT(4) fix
  (a dedicated `regs_t8132` reg_info, upstream commit `0f221fc`) is the whole
  fix. On runs 18–23:
  - both ports **leave `LINKSTS BUSY`** (`0x8300020c/0x83000204` → `0xab000208/
    0xab000200`, "BUSY CLEARED after 0.00s"),
  - `STATUS +0x804` `0x05 → 0x0d` (PHY out of reset),
  - `pcie_init() → 0`,
  - **ECAM config space answers for the first time**: root ports
    `00:00.0` and `00:02.0` = `VID:DID 106b:100c class=06.04.00` (Apple PCI
    bridges) instead of `0xffffffff`.
  - The old 37-boot `phy_ip 0x497040038` wedge is **side-stepped** (runs skip that
    window); run 17, which deliberately touches it, still wedges there — but we do
    **not** need it to bring the link/bridges up.
- **SMP: the RVBAR wall fell, a deeper one is exposed, and it does NOT matter for
  this goal.** The `apple_sysregs_unlocked` fix removed the old `Failed! RVBAR is
  locked` message; secondaries now fail later, at the spin-table poll (bare
  `Failed!`, `smp.c:170`) — the core is strobed but never checks in. **A
  single-threaded TCP server needs exactly one core, which we have. SMP is off the
  critical path — do not spend more runs on it.**

### Where we actually are on the PLAN_2 phase ladder

| Phase | PLAN_2 milestone | Status |
| --- | --- | --- |
| 1 Recon | ADT/NIC/DART/MAC dumped | ✅ done (`m4_recon/`) |
| 2 Payload (`p.call`) | kernel runs as m1n1 payload | ✅ done (hello-t8132) |
| 3 PCIe reach | link up + NIC in ECAM | 🟡 **link+bridges up; NIC not yet enumerated** ← we are here |
| 4 DART | IOMMU map for NIC DMA | ⬜ |
| 5 NIC driver | rings/MAC/link/TX | ⬜ (chip identity still unknown) |
| 6 ARP | arping replies | ⬜ |
| 7 lwIP | ping replies | ⬜ |
| 8 TCP | `nc … 3333` → Hello World | ⬜ (the goal) |

### The single fact that gates everything next

The NIC (`/arm-io/apcie/pci-bridge2/lan-1gb`, MAC `d0:11:e5:71:81:dc`,
`iommu-parent → dart-apcie2`) has **no VID:DID, no compat, no reg in the ADT** —
only `enet_config=1`, `device_type=lan0`. **We cannot identify the chip (and thus
cannot write a driver) until we read its PCI config space.** The bridges
enumerate but show `primary=0 secondary=0 subordinate=0` — bus numbers were never
assigned, so the ECAM walk never descends behind `00:02.0` to where the NIC lives.

**Therefore the immediate next step is PCIe bus-number programming**, and its
payoff is the chip ID that unblocks the entire driver phase.

---

## Phase 3.5 — Descend behind the bridge, identify the NIC  ← DO THIS FIRST

Goal: get a real VID:DID + class + BAR0 for the device on `pci-bridge2`'s
downstream bus. This is **standard PCIe enumeration**, not Apple RE.

### 3.5.1 Program bridge bus numbers, then re-walk (no reflash)

In `perstn.py`'s `ecam_walk` (it already reaches root bus 0 cleanly, exc_delta 0):
after `pcie_init()` returns and BUSY clears, for the active root port `00:02.0`
(and `00:00.0`):
1. Write the type-1 bridge bus registers at config `+0x18`:
   `primary=0`, `secondary=1` (2 for the next bridge), `subordinate=0xff`
   (open the window; tighten later). This is a single `write32(cfg+0x18, …)`.
2. Enable the bridge command reg (`cfg+0x04 |= MEM|BUSMASTER = 0x6`).
3. **Re-walk bus 1** (`ecam_base + (bus<<20) + (dev<<15)`), reading `VID:DID`
   (`+0x00`), `class` (`+0x08`), header type. Log every non-`0xffffffff`.
4. For the class-0x02 device found: read BAR0 (`cfg+0x10`), size it (write
   `0xffffffff`, read back, restore), record the MMIO base/size.

Reuse the existing wrappers: `_read32_live`, the `guarded()`/`check_alive()`
liveness pattern, the `ecam_base = 0x1cb0000000` resolution already in
`ecam_walk`. Add a `--enumerate-bridges` flag; keep every read/write guarded so a
partial link leaves `/tmp/m4-recon/nic-runtime.txt` behind (not a wedge).

### 3.5.2 If the NIC does NOT appear behind the bridge

Ordered fallbacks (each cheap, no reflash):
- **Link not fully trained.** `LINKSTS = 0xab000208` is out of BUSY but not the
  classic UP encoding (bit0 set / bit2 clear). Check `PORT_STATUS +0x804`
  (`0x0d`) and poll `LINKSTS bit0` after bus-number programming; the endpoint
  may need the LTSSM to reach L0 first. The RUN 21 `perst_resequence` +
  `setup_refclk` machinery already exists — run it, then re-enumerate.
- **The NIC needs the phy_ip window after all.** Only *now* is it worth
  re-attacking `0x497040038`. With BIT(4) the PHY is out of reset — retest
  whether phy_ip decodes from the *new* post-BIT(4) state (it was never tried
  from a non-wedged PHY). This is a genuine open question, not the old dead axis.
- **Endpoint power / PERST# timing.** `pci-bridge2` has `perst=gpio0[165]`,
  `clkreq=gpio0[162] mode 2`. Re-check the ADT `t-refclk-to-perst=100` /
  `perst-to-config=100` ordering (RUN 21 lineage).

### 3.5.3 Milestone M3.5

`perstn.py --enumerate-bridges` prints a real `VID:DID class=02.00.00` for a
device on bus 1 behind `pci-bridge2`, plus a plausible non-`0xffffffff` BAR0.
**Commit that VID:DID to `m4_recon/recon-summary.md` (closes Q2/Q3).** The VID
decides the driver in Phase 5 and whether we stay on the PCIe path or take the
R2 fallback.

---

## Phase 4 — DART for the NIC (IOMMU DMA mapping)

Now safe (fabric is powered + linked). Deliverable `Scripts/m1n1/dart_up.py`:
`DART.from_adt(u, "arm-io/dart-apcie2")` (compat `dart,t8110`), `.initialize()`,
`p.memalign(0x4000, 0x40000)` for a 256 KiB DMA arena,
`dart.iomap_at(0, 0x40000, phys, 0x40000)`, then `dart.dump_all()` to confirm the
L1/L2 entry `iova → phys`. No kernel changes. Matches PLAN_2 §Phase 4 verbatim;
DART is stable across SoCs (`dart,t8110` is the same IP m1n1 already drives).

**M4:** `dart.dump_all()` shows the mapping. Note: `dart-apcie2` MMIO is at
`0x492000000` and *overlaps port-2 `ctrl_lo`* — only touch it via the DART API,
never raw writes (PLAN.md §3.3.2 warning).

---

## Phase 5 — NIC driver (chip-dependent; branch on M3.5's VID)

**This phase is only fully plannable once M3.5 gives the VID:DID.** Two branches:

- **5A — discrete PCIe NIC with a known family** (Broadcom `tg3`, Marvell,
  Aquantia, Realtek): write `src/nic_<chip>.c` behind the chip-agnostic
  `include/nic.h` (`nic_init/rx/tx/link_status`). Follow PLAN_2 §Phase 5
  sub-milestones M5a–M5e (chip-ID reg → reset → MAC filter → link → TX one frame
  host `tcpdump` sees). LOC budget 400–800. Extend `kmain(nic_mmio, dma_iova, ba)`
  and `upload_and_call.py` to pass `nic_mmio` (BAR0 from M3.5) + `dma_iova` (from
  Phase 4).
- **5B — Apple SoC-integrated NIC with no docs** (the R2 risk): if the VID is
  Apple (`0x106b`) and there's no register doc, **pause and decide with the user**
  between (a) RE the MAC from scratch (1–3 months) or (b) the **USB-CDC-ECM
  fallback** (PLAN_2 §14 R2, ~3–4 weeks): chainload out of m1n1, bring up DWC3
  (`m1n1/src/usb_dwc3.c` reference), expose an ECM gadget, run lwIP over `usb0`.
  This changes the downstream plan; do not commit to it silently.

**M5:** M5e passes (a TX frame from the M4 shows up in host `tcpdump`).

---

## Phases 6–8 — Network stack (chip-independent once the NIC TXes/RXes)

Straight from PLAN_2, unchanged — these are well-trodden and not M4-specific:

- **Phase 6 — ARP** (`src/arp.c`, EtherType 0x0806): `arping -c3` from host → 3
  replies; `ip neigh` shows the M4 MAC. Use `MY_IPV4 = 192.168.0.99` (outside the
  DHCP pool; the `.246` macOS lease is stale — PLAN_2 R6).
- **Phase 7 — lwIP** (`NO_SYS=1`, TCP+ARP+ICMP only, vendored `STABLE-2_2_0`):
  `src/net.c` glue (`linkoutput → nic_tx`, poll `nic_rx → ethernet_input`),
  `sys_now()` from `CNTVCT_EL0`. **M7:** `ping -c3` → 3 replies.
- **Phase 8 — TCP hello world** (`src/hello.c`, lwIP raw API: `tcp_new` →
  `bind(3333)` → `listen` → `accept` → `tcp_write("Hello World\n")` → `close`):
  **M8a `nc <ip> 3333` → `Hello World`** = **the goal.** M8b: 100-round loop, PCB
  count returns to 0. M8c: `ping` still works while serving.

---

## How far off are we?

**Much closer than a week ago.** The PCIe wall that ate ~30 runs is down; config
space answers. Remaining work, in effort order:

1. **Bridge bus-number programming (Phase 3.5)** — hours to a day. **This is the
   next run.** Its output (the NIC's VID:DID) is the pivot for everything after.
2. **DART (Phase 4)** — hours; standard `dart,t8110`.
3. **NIC driver (Phase 5)** — the real unknown: days if it's a documented discrete
   chip (5A), a fork in the road if it's undocumented Apple silicon (5B). M3.5
   resolves which.
4. **ARP → lwIP → TCP (Phases 6–8)** — days total; portable, not M4-specific.

**Realistic:** if the NIC is a documented discrete PCIe chip, a working
`nc → Hello World` is on the order of **1–2 focused weeks** of iteration. If it's
undocumented Apple-integrated silicon, Phase 5 dominates and we should seriously
weigh the USB-CDC-ECM fallback. **The one experiment that collapses this
uncertainty is Phase 3.5 — do it first.**

---

## Guardrails (carried from PLAN.md/PLAN_2.md)

- **One milestone per phase; no forward progress until it passes** (PLAN_2 §0).
- **All apcie/DART MMIO is now reachable** *only because `pcie_init` completed* —
  keep every new read/write `guarded()` + `check_alive()` so a partial state
  leaves a log, not a wedge. `ctrl_lo`/`dart-apcie2` overlap: DART API only.
- **`--require-build` guard:** new banner is `v1.6.0-42-gdf2ae61`; use substring
  `v1.6.0-42-g`. The old `rc1-59-g`/`rc1-60-g` guards will abort — update any arm
  you reuse (append `--require-build=v1.6.0-42-g` to override; argparse last-wins).
- **You run privileged/hardware commands.** I print exact commands and read the
  logs back; no runs are executed for you here.
- **m1n1 source lives at `/home/ahmed/Projects/C/embedded/m1n1`** (branch
  `t8132-rebase`); read the C directly rather than guessing (ref-m4-repos.md).
- **SMP is parked.** Revisit only if a future phase genuinely needs a second core
  (this milestone does not).
