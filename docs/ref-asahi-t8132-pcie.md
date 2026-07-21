---
name: ref-asahi-t8132-pcie
description: What upstream Linux (asahi-wip) and Asahi m1n1 do vs do NOT do for t8132 (Apple M4) PCIe bring-up, with exact source citations, per-SoC delta table, and ranked RUN 3 hypothesis candidates
metadata:
  type: reference
---

Snapshot of the Asahi source-of-truth for Apple M4 (t8132) PCIe bring-up, taken 2026-07-21 against the untouched `asahi-wip` Linux clone at `/home/ahmed/Projects/C/embedded/untouched_asahi_linux/` and the local m1n1 fork at `/home/ahmed/Projects/C/embedded/m1n1/`. Purpose: give [[project-m4-pcie-bringup]] the upstream context it needs to select RUN 3 candidates once the RUN 2 result lands. See [[ref-m4-repos]] for tooling / log paths.

**Bottom line first.** No functioning t8132 PCIe support exists anywhere in upstream, asahi-wip, asahi-soc/for-next, or any lore thread. The Linux driver `pcie-apple.c` has never claimed `apple,t8132-pcie`. The DT binding never listed it. asahi-wip's `t8132.dtsi` contains no PCIe/NVMe/DART nodes. Asahi's `drivers/phy/apple/` has only `atc.c` (USB-C PHY) — there is no Apple PCIe PHY driver. Bring-up work on t8132 lives entirely in `m1n1/src/pcie.c`, and the current t8132 code path there is empirical / SError-prone (commit history below). This is not a "port a working driver over" problem; it's a "figure out what iBoot programs that no one else has needed to figure out" problem.

---

## 1. What upstream Linux does NOT do (pre-conditions it assumes iBoot / m1n1 satisfied)

The `pcie-apple.c` driver contains no code that programs the PHY analog block. It expects `phy_ip` (the per-port analog PLL register bank) to already be tuned by the time it runs. Specifically, `apple_pcie_setup_refclk()` at `pcie-apple.c:508-542` only twiddles four bits at `phy_base + PHY_LANE_CFG (0x00000)` — REFCLK0REQ/ACK handshake, REFCLK1REQ/ACK handshake, REFCLKEN — and never touches offsets ≥ 0x08 of the PHY window. There is no PLL calibration application, no fuse-bit copy from OTP, no DCO tuning, no auspma setup. Any of that must already be done.

The `sync_ip`-style register block that our j773g bring-up wedges on (`phy_ip_base + 0x38`) is likewise never written or read by the Linux driver. The Linux `hw_info` struct at `pcie-apple.c:153-162` has fields for port config, port PERST, port MSI, RID/SID, etc. — none for PHY analog. Fields exposed:

```
struct hw_info {
    u32 phy_lane_ctl;       // 0x004 on t8103, 0 on t602x
    u32 port_msiaddr;
    u32 port_msiaddr_hi;
    u32 port_refclk;        // 0x810 on t8103, 0 on t602x
    u32 port_perst;         // 0x814 on t8103, 0x82c on t602x
    u32 port_rid2sid;
    u32 port_msimap;
    u32 max_rid2sid;
};
```

The two instances that exist are `t8103_hw` (`pcie-apple.c:164-173`) and `t602x_hw` (`pcie-apple.c:175-185`). Compat map at `pcie-apple.c:992-996` is only `apple,t6020-pcie` and `apple,pcie`. On t602x, `phy_lane_ctl = 0` — meaning the CFGACC handshake at `pcie-apple.c:514-515, 533-534` is skipped entirely on newer chips.

Concretely, on a t8132 boot into Linux, the driver would:

- match `apple,t6020-pcie` via the `apple,t8122-pcie -> apple,t6020-pcie` fallback pattern (see `Documentation/devicetree/bindings/pci/apple,pcie.yaml:36-47`) — assuming a t8132 DT binding follows the same fallback-to-t6020 pattern
- pick up per-port `phy` reg items via `platform_get_resource_byname(..., "phy%d")` at `pcie-apple.c:684-689`, with fallback to `CORE_PHY_DEFAULT_BASE(port) = 0x84000 + 0x4000 * port` at `pcie-apple.c:52` (unlikely to hit on t602x-style layouts)
- check `PORT_LINKSTS_UP` at `pcie-apple.c:692` and, if set, SKIP `apple_pcie_setup_link()` entirely — assuming u-boot (or in our case, m1n1) already brought the port up
- otherwise call `apple_pcie_setup_refclk()` and expect it to succeed on unmodified iBoot-programmed PHY state

**Conclusion for our bring-up:** upstream Linux tells us nothing new about the phy_ip programming sequence. Everything we need has to come from RE'ing what iBoot does, or from m1n1's own git history / ADT tunables. But upstream does tell us — via absence — that `phy_ip+0x38` (and all of the PHY analog window ≥ 0x08) is expected to be programmed ONCE by the boot chain and never touched again by the driver.

## 2. What upstream Linux DOES do (post-bringup steps, with citations)

Reading `pcie-apple.c` end-to-end, the post-bringup call chain is:

- `apple_pcie_probe()` at `pcie-apple.c:952-990` — allocates bridge, hooks `apple_pcie_cfg_ecam_ops`
- `apple_pcie_init()` at `pcie-apple.c:896-915` — walks child nodes (ports), calls `apple_pcie_setup_port()` per port
- `apple_pcie_setup_port()` at `pcie-apple.c:646-747` — the meat:

```
- resolve port_base (idx = reg >> 11) and port->phy base
- link_stat = readl(port + PORT_LINKSTS)             # pcie-apple.c:692
- if not UP:
    setup_link()                                     # 694
- clear PORT_REFCLK_CGDIS or set PHY_LANE_CFG_REFCLKCGEN   # 699-702
- clear PORT_APPCLK_CGDIS                            # 704
- setup_irq                                          # 706
- populate RID/SID map (writes 0xbad1d, reads back)  # 711-715
- register per-port IRQs                             # 727
- if not UP: writel PORT_LTSSMCTL_START; wait_for_completion  # 731-742
```

- `apple_pcie_setup_link()` at `pcie-apple.c:557-644` — the sequence with cited numeric constants:

```
- devm_fwnode_gpiod_get "reset" -> reset      # PERST# main, asserted HIGH  L575
- probe up to 3 aux PERST GPIOs               # 579-592
- devm_fwnode_gpiod_get "pwren" -> pwren      # 595-602 (optional)
- rmw_set(PORT_APPCLK_EN)                     # 604
- gpiod_set(reset, 1)                         # 607   assert PERST
- gpiod_set(aux_reset[i], 1)                  # 608-609
- gpiod_set(pwren, 1)                         # 612
- apple_pcie_setup_refclk()                   # 614   -- SEE section 2b
- msleep(100) if pwren else usleep 100-200    # 622-625  Tperst-clk min 100us; Tpvperl min 100ms
- rmw_set(PORT_PERST_OFF, port+port_perst)    # 628
- gpiod_set(reset, 0)                         # 629   deassert PERST
- msleep(100)                                 # 634   Tperst 100ms per PCIe r5.0 6.6.1
- readl_relaxed_poll_timeout(PORT_STATUS, PORT_STATUS_READY, 100, 250000)  # 636-637
```

- `apple_pcie_setup_refclk()` at `pcie-apple.c:508-542`:

```
if (hw->phy_lane_ctl)                                             # t8103 only
    rmw_set(PHY_LANE_CTL_CFGACC, port->phy + phy_lane_ctl)
rmw_set(PHY_LANE_CFG_REFCLK0REQ, port->phy + PHY_LANE_CFG)         # 517
readl_poll_timeout(port->phy + PHY_LANE_CFG, stat, stat & REFCLK0ACK, 100, 50000)   # 519-521
rmw_set(PHY_LANE_CFG_REFCLK1REQ, port->phy + PHY_LANE_CFG)         # 525
readl_poll_timeout(..., REFCLK1ACK, 100, 50000)                    # 526-528
if (hw->phy_lane_ctl)
    rmw_clear(PHY_LANE_CTL_CFGACC, port->phy + phy_lane_ctl)
rmw_set(PHY_LANE_CFG_REFCLKEN, port->phy + PHY_LANE_CFG)           # 536
if (hw->port_refclk)                                                # t8103 only
    rmw_set(PORT_REFCLK_EN, port->base + hw->port_refclk)          # 539
```

Salient constants used, with definitions elsewhere in the file:

- `PHY_LANE_CFG = 0x00000` in `port->phy` window (`pcie-apple.c:54`)
- `PHY_LANE_CTL = 0x00004` — CFGACC BIT(15) (`pcie-apple.c:61-62`)
- `REFCLK0REQ = BIT(0)`, `REFCLK1REQ = BIT(1)`, `REFCLK0ACK = BIT(2)`, `REFCLK1ACK = BIT(3)`, `REFCLKEN = BIT(9)|BIT(10)`, `REFCLKCGEN = BIT(30)|BIT(31)` (`pcie-apple.c:55-60`)
- `PORT_STATUS = 0x804`, `PORT_STATUS_READY = BIT(0)` (`pcie-apple.c:109-110`)
- `PORT_LINKSTS = 0x208`, `PORT_LINKSTS_UP = BIT(0)`, `PORT_LINKSTS_BUSY = BIT(2)` (`pcie-apple.c:91-93`)
- `PORT_LTSSMCTL = 0x080`, `PORT_LTSSMCTL_START = BIT(0)` (`pcie-apple.c:64-65`)
- `PORT_APPCLK = 0x800`, `PORT_APPCLK_EN = BIT(0)` (`pcie-apple.c:106-107`)
- `PORT_PERST = 0x814` (t8103 = `port_perst`), `PORT_T602X_PERST = 0x82c` (t602x) (`pcie-apple.c:114, 137`)

Core / shared regs the driver defines but does NOT touch on newer SoCs:

- `CORE_RC_PHYIF_CTL = 0x024` with `RUN = BIT(0)` (`pcie-apple.c:41-42`)
- `CORE_RC_PHYIF_STAT = 0x028` with `REFCLK = BIT(4)` (`pcie-apple.c:43-44`)
- Both were written by the driver historically; commit `de9637c9f782 PCI: apple: Drop poll for CORE_RC_PHYIF_STAT_REFCLK` removed the STAT poll (was T81xx-only anyway).

Nothing in this driver would help us past our current wedge. It's all downstream of what we can't reach.

### 2b. Where the driver DIVERGES per SoC — the SoC delta table

Verified from `pcie-apple.c` current state + git log, and (for t8132) inferred:

| Behavior                                          | t8103 (M1) | t8112 (M2) | t600x (M1 Pro/Max/Ultra) | t602x (M2 Pro+) | t8122 (M3) | t8132 (M4)     |
| --- | --- | --- | --- | --- | --- | --- |
| `hw_info` chosen                                  | `t8103_hw` | `t8103_hw` | `t8103_hw`               | `t602x_hw`      | `t602x_hw` (via `apple,t6020-pcie` fallback) | UNDEFINED — driver has no compat match |
| Compat string(s)                                  | `apple,t8103-pcie`, `apple,pcie` | `apple,t8112-pcie`, `apple,pcie` | `apple,t6000-pcie`, `apple,pcie` | `apple,t6020-pcie` | `apple,t8122-pcie`, `apple,t6020-pcie` | ADT declares `apcie,t8132`; NOT in DT binding YAML |
| `phy_lane_ctl` (CFGACC)                           | 0x004      | 0x004      | 0x004                    | 0                | 0          | N/A (no driver) |
| `port_refclk` (`PORT_REFCLK_EN` needed?)          | 0x810      | 0x810      | 0x810                    | 0 — skipped      | 0 — skipped| N/A |
| `port_perst` offset                               | 0x814      | 0x814      | 0x814                    | 0x82c            | 0x82c      | N/A |
| Per-port PHY reg items ("phy0".."phy3" resources) | absent (uses `CORE_PHY_DEFAULT_BASE`) | absent | absent | present per `apple,t6020-pcie` YAML `if:` (`apple,pcie.yaml:135-143`) | present | inferred present (t8132 ADT declares per-port PHY reg items — see recon-summary.md 25 regs / 3 ports / shared 7) |
| Driver applies any tunables to phy_ip / phy_shared / axi_base? | **NO** | **NO** | **NO** | **NO** | **NO** | **NO** — driver has no compat, so nothing runs |
| Driver expects PHY analog already programmed by bootloader | YES | YES | YES | YES | YES | YES |

Two structural facts extrapolate cleanly from `4e639f11d6e0 PCI: apple: Add T602x PCIe support` and `80b31fbbcac4 PCI: apple: Move port PHY registers to their own reg items`:

- Every SoC-addition commit changes ONLY per-port register offsets. It never adds new init steps.
- The reason t602x needed per-port PHY reg items (`phy0`, `phy1`, etc.) is that on t602x the PHY windows are non-contiguous — no longer at `0x84000 + 0x4000*port` from `pcie->base`. t8132's ADT already carries per-port PHY reg items (see `m4_recon/adt.txt:1443` — 25 reg entries with `#ports = 3`, `shared_reg_count = 7`), which matches the same design.

So a hypothetical `apple,t8132-pcie` compat added to the Linux driver would look like an `hw_info` almost identical to `t602x_hw` — same offsets, same `max_rid2sid = 512` size class, same `phy_lane_ctl = 0` (no CFGACC), same `port_perst = 0x82c`. **It would not add anything that helps our bring-up.**

## 3. atc.c CIO3PLL_CORE application pattern (reference for what a proper PCIe PHY init looks like)

`drivers/phy/apple/atc.c` is Asahi's only landed Apple PHY driver, for Type-C USB / DP / Thunderbolt. It shares the CIO3PLL analog block architecture with PCIe. Its ordering is the closest analog we have to what a proper `pcie-apple-phy` driver would look like on t8132.

### 3a. Tunable library format (`drivers/soc/apple/tunable.c`)

`tunable.c:37-56` parses each ADT tunable entry as three `u32`s: offset, mask, value — **12 bytes per entry**. `tunable.c:62-75` `apple_tunable_apply` does a read/modify/write but with a `if (val != old_val)` guard — it SKIPS the write when the register already reads the target. This matches the "SKIP-NOOP" behavior we observed in RUN S/1's naked-apply audit (RUN S entries pre=target were skipped).

Contrast m1n1's `struct tunable_local` at `m1n1/src/tunables.c:71-76`: `u32 offset; u32 size; u64 mask; u64 value` — **24 bytes per entry**. **Different wire format.** The Linux 12-byte tunables are named `apple,tunable-*` and only appear on nodes that use the DT-shim conversion path. The 24-byte legacy `apcie-*-tunables` on t8132's ADT are m1n1-format and are meant for m1n1 to consume; no Linux code has ever parsed them for PCIe.

### 3b. atc.c CIO3PLL bring-up sequence

`atc.c:1725-1788` `atcphy_configure()`:

```
atcphy_power_on()                                    # atc.c:1694-1723:
  atcphy_usb2_power_on()                             # unrelated to PCIe
  core_set32(ATCPHY_MISC, MISC_RESET_N)              # 1701
  core_set32(POWER_CTRL, POWER_SLEEP_SMALL)          # 1703
  readl_poll_timeout(POWER_STAT, SLEEP_SMALL, 100, 100000)  # 1704
  core_set32(POWER_CTRL, POWER_SLEEP_BIG)            # 1711
  readl_poll_timeout(POWER_STAT, SLEEP_BIG, 100, 100000)
  core_clear32(POWER_CTRL, POWER_CLAMP_EN)           # 1719
  core_set32(POWER_CTRL, POWER_APB_RESET_N)          # 1720

atcphy_apply_tunables(mode)                          # atc.c:877-918:
  apple_tunable_apply(regs.core,   common[0])        # 882   -- "common-a"
  apple_tunable_apply(regs.axi2af, axi2af)           # 883
  apple_tunable_apply(regs.core,   common[1])        # 884   -- "common-b" (axi2af sandwiched between)
  apple_tunable_apply(regs.core,   lane_usb3/dp/usb4[0]) # 894-911 lane-specific
  apple_tunable_apply(regs.core,   lane_usb3/dp/usb4[1])

core_set32(AUSPLL_FSM_CTRL, 0x1fe000)                # atc.c:1743   -- PLL FSM kick
core_set32(AUSPLL_APB_CMD_OVERRIDE, UNK28)           # 1744

set32(CFG0, COMMON_SMALL_OV); udelay(10)             # 1746-1747
set32(CFG0, COMMON_BIG_OV);   udelay(10)             # 1748-1749
set32(CFG0, COMMON_CLAMP_OV); udelay(10)             # 1750-1751

mask32(SLEEP_CTRL, TX_SMALL_OV, 3); udelay(10)       # 1753-1755
mask32(SLEEP_CTRL, TX_BIG_OV,   3); udelay(10)       # 1756-1758
mask32(SLEEP_CTRL, TX_CLAMP_OV, 3); udelay(10)       # 1759-1761

mask32(CFG0, RX_BIG_OV,   3); udelay(10)             # 1763-1765
mask32(CFG0, RX_SMALL_OV, 3); udelay(10)             # 1766-1768
mask32(CFG0, RX_CLAMP_OV, 3); udelay(10)             # 1769-1771

if DP mode: atcphy_enable_dp_aux                     # 1774-1775

core_set32(CIO3PLL_CLK_CTRL, CIO3PLL_CLK_PCLK_EN)    # 1778 -- CIO3PLL enable step 1
core_set32(CIO3PLL_CLK_CTRL, CIO3PLL_CLK_REFCLK_EN)  # 1779 -- CIO3PLL enable step 2

atcphy_configure_lanes(mode)                         # 1780
core_set32(POWER_CTRL, POWER_PHY_RESET_N)            # 1783
```

### 3c. Key CIO3PLL / AUSPLL register map (in the CORE window)

From `atc.c:47-124`:

- `AUSPLL_FSM_CTRL = 0x1014`
- `AUSPLL_APB_CMD_OVERRIDE = 0x2000` (bits: REQ 0, ACK 1, UNK28 28, CMD 27:3)
- `AUSPLL_FREQ_DESC_A/B/C = 0x2080/0x2084/0x2088`
- `AUSPLL_DCO_EFUSE_SPARE = 0x222c` (RODCO_ENCAP_EFUSE 10:9, RODCO_BIAS_ADJUST_EFUSE 14:12)
- `AUSPLL_FRACN_CAN = 0x22a4` (DLL_START_CAPCODE 18:17)
- `AUSPLL_CLKOUT_MASTER = 0x2200` (PCLK_DRVR_EN 2, PCLK2_DRVR_EN 4, REFBUFCLK_DRVR_EN 6)
- `AUSPLL_BGR = 0x2214` (CTRL_AVAIL 0)
- `AUSPLL_CLKOUT_DTC_VREG = 0x2220` (VREG_ADJUST 16:14, VREG_BYPASS 7)
- **`CIO3PLL_CLK_CTRL = 0x2a00`** — PCLK_EN 1, REFCLK_EN 5
- **`CIO3PLL_DCO_NCTRL = 0x2a38`** — DCO_COARSEBIN_EFUSE0 6:0, DCO_COARSEBIN_EFUSE1 23:17
- `CIO3PLL_FRACN_CAN = 0x2aa4` — DLL_CAL_START_CAPCODE 18:17
- `CIO3PLL_DTC_VREG = 0x2a20` — DTC_VREG_ADJUST 16:14

**Note the offset-0x38 alignment:** CIO3PLL_CLK_CTRL is at 0x2a00 and CIO3PLL_DCO_NCTRL is at 0x2a38. If PCIe's `phy_ip` window is laid out analogously (CIO3PLL base at phy_ip+0), then `phy_ip+0x38` is the DCO calibration register receiving the per-die "coarsebin efuse" values. Reading from this register before the PLL clock has been enabled (CLK_CTRL PCLK_EN + REFCLK_EN) would indeed AXI-stall — the DCO block has no clock to respond with.

That's the strongest source-of-truth signal in this whole reference: **`phy_ip+0x38` on t8132 PCIe is very likely the DCO NCTRL register of a CIO3PLL_CORE-shaped block, and the reason it stalls is that the CIO3PLL clock has not been enabled yet.** The atc.c ordering fires `CIO3PLL_CLK_CTRL PCLK_EN` (1778) and `REFCLK_EN` (1779) BEFORE any lane configuration or lane-tunable writes — and lane tunables would sit at higher offsets. On our replay we've been trying to write DCO NCTRL first (via `apcie-phy-ip-pll-tunables` entry #0 at `+0x38`) with no upstream clock enable.

### 3d. Whether atc.c requires an SMC/RTKit handshake before PLL enable

Verified by reading atc.c end-to-end: **NO**. atc.c never talks to SMC, RTKit, mailbox, or SEP. Its only external dependency is on the tunable library (`apple_tunable_apply`) and the reset controller. Its entire init runs from bare CPU writes into the ATCPHY MMIO window. The chip-level power gate (`ps_atc0_cio`, etc.) is opened by the DT power-domain runtime PM before probe.

For PCIe on t8132, that means the RUN 1 posture (SMC gP0d=0x800001 done, PMGR `APCIE_PHY_SW` walked to ACTIVE via Phase D) is already sufficient at the power-fabric level. What we're missing is not another handshake — it's the analog equivalent of atc.c's steps 1743-1779.

## 4. t8132 DTS + PMGR chain (upstream vs t8122 vs t6030)

`arch/arm64/boot/dts/apple/t8132.dtsi:12-437` is minimal: CPUs (Donan e/p), AIC3 (`apple,t8132-aic3`, `apple,t8122-aic3`), PMGR, WDT, pinctrl (nub/smc/aop/ap), I2C, PWM, UART. **No PCIe / NVMe / DART / SMC / ANS nodes.** `t8132-j773g.dts` is 25 lines and only overrides the framebuffer power-domain to keep HDMI up. There is no on-tree Linux driver path that would touch PCIe on this SoC.

The t8132 PMGR chain for PCIe, from `t8132-pmgr.dtsi`:

```
ps_apcie_gp       @ 0x4d0    -- no parent
ps_apcie_sys_gp   @ 0x4d8    -- parent: ps_apcie_gp
ps_ans            @ 0x538    -- no parent
ps_apcie_st       @ 0x540    -- parent: ps_ans
ps_apcie_sys_st   @ 0x548    -- parents: ps_ans, ps_apcie_st
ps_apcie_phy_sw   @ 0x550    -- parents: ps_apcie_sys_st, ps_apcie_sys_gp     (NOT always-on)
```

Compare `t8122-pmgr.dtsi:900-907`:

```
ps_apcie_phy_sw   @ 0x4a0    -- NO parents, apple,always-on  /* macOS does not turn this off */
```

And `t6030-pmgr.dtsi:1145-1152` — identical to t8122 (always-on, no parents).

**The t8132 phy_sw gate is materially different from every previous chip's phy_sw gate:** it is NOT always-on, and it requires BOTH `sys_st` AND `sys_gp` chains to be up first. On t8122 and t6030, macOS never touched it (always-on). On t8132, macOS presumably manages it explicitly. **This is a t8132-specific design change and the only structural PMGR difference on the PCIe path.**

Cross-check with our runtime (`Scripts/m1n1/logs/1/nic-runtime.txt:399-455`):

- Phase 0 snapshot (before Phase D poke): `APCIE_PHY_SW ps@0x380700550 = 0x1400024f` — target=0xf but actual=0x4 → **OFF**. All 5 virtual `-V` aggregators (ANS-V, APCIE-GP-V, APCIE-SYS-GP-V, APCIE-ST-V, APCIE-SYS-ST-V) show `VIRTUAL (no PS reg)` — they aggregate other gates through `power_gates` chain, they aren't PMGR registers themselves.
- Underlying non-virtual gates: `APCIE_GP` ON, `APCIE_SYS_GP` ON, `ANS` ON, `APCIE_ST` ON, **`APCIE_SYS_ST` OFF (actual=0x0)**, `APCIE_PHY_SW` OFF.
- Phase D poke sequence: wrote `APCIE_SYS_ST` first (parent of phy_sw), converged to actual=0xf; then wrote `APCIE_PHY_SW`, converged to actual=0xf.
- Phase E verification: all three real gates (`APCIE_SYS_GP`, `APCIE_SYS_ST`, `APCIE_PHY_SW`) ACTIVE.
- Phase E rc/axi sanity: `rc_base+0 = 0x00040000`, `rc_base+0x054 = 0x00000140`, `rc_base+0x058 = 0x00000001`, `axi_base+0 = 0x0000001c`, `axi_base+0x600 = 0x0`. All alive.

So `APCIE_PHY_SW` IS being ungated by perstn.py before Phase F starts. The "phy_sw is not powered" hypothesis is refuted by our own log evidence. That's still worth writing down here because it's not obvious from the code alone.

**But note:** just because the phy_sw *power* gate is ACTIVE doesn't mean the phy_ip *clock tree* inside that domain is running. `ps_apcie_phy_sw` powers the analog block. What starts the CIO3PLL analog PLL itself is presumably the `CIO3PLL_CLK_CTRL` PCLK_EN + REFCLK_EN write pair (analog of `atc.c:1778-1779`). We have never done that on PCIe. See Section 8 for RUN 3 candidate #1.

## 5. j773g ADT-declared apcie tunables

From `Scripts/m1n1/logs/1/nic-runtime.txt:516-547`, the 30 properties on `/arm-io/apcie`. The 6 tunable-shaped ones:

| Property                            | Applied by m1n1 pcie.c? | Applied by Linux pcie-apple.c? | Notes |
| --- | --- | --- | --- |
| `apcie-axi2af-tunables`             | YES — `pcie.c:432` (`tunables_apply_local`, reg_idx=`axi_idx`=4) | never | 58 entries. RUN O showed the m1n1 applicator silently no-ops these. RUN S naked-apply landed 14 STUCK / 2 NO-OP / 42 SKIP-NOOP. |
| `apcie-common-tunables`             | YES — `pcie.c:443` (reg_idx=`rc_idx`=1) | never | RUN O also showed silent no-op on this via broken applicator. |
| `apcie-phy-tunables`                | YES — `pcie.c:454` (reg_idx=`phy_idx`=2) | never | RUN O showed writes to phy_shared+0 DID land via this path (`0xf7c03090 -> 0xf3c03090`). So the applicator works for reg_idx=2 but not 1/4. |
| `apcie-phy-ip-pll-tunables`         | YES on T8140 codepath (`pcie.c:518`, `tunables_apply_local_addr` with phy_ip_base) — via broken applicator | never | This is what wedges at entry #0 `+0x38` in every RUN. |
| `apcie-phy-ip-auspma-tunables`      | YES on T8140 codepath (`pcie.c:522`, ditto) | never | Never reached — pll wedge blocks it. |
| `apcie-cio3pllcore-tunables`        | **NO** — no code path applies this | never | 7 entries. Entries #0-#3 at offsets 0x00/0x24/0x28/0x38 SKIP-NOOP against axi_sub5 pre-values (RUN R). Entries #4-#6 at 0x4c/0xe8/0x100 tested in RUN 2 against `axi_sub5_base`. |
| `apcie-pcieclkgen-tunables`         | **NO** — no code path applies this | never | 1 entry at offset 0. RUN S naked-applied to `axi_sub5_base+0`: `0x00000a01 -> 0x00000a21` STUCK. |

The `apcie-cio3pllcore-tunables` and `apcie-pcieclkgen-tunables` are t8132-specific novelties that no code (upstream Linux, Asahi, m1n1, u-boot) has ever consumed. Confirmed by:

```
grep -r "cio3pllcore\|pcieclkgen" /home/ahmed/Projects/C/embedded/untouched_asahi_linux  # zero matches
grep -r "cio3pllcore\|pcieclkgen" /home/ahmed/Projects/C/embedded/m1n1                    # zero matches
```

These properties describe writes that iBoot (on macOS) is presumed to perform. On our bare-metal boot from m1n1, no equivalent code has ever run.

## 6. m1n1 fork t8132 lineage

Local commits touching `src/pcie.c` for t8132, oldest first (from `git log --all --oneline -- src/pcie.c`):

```
ee9b318 added PCIE for t8132                                  -- earliest, framework
546237d pcie: initial t8132 (base M4) support                 -- adds apcie,t8132 clause, maps to regs_t8140
7a1a083 pcie: t8132 maps to regs_t8122; keep initializing remaining ports on port failure  -- data-driven swap to regs_t8122
dc25f2f pcie: t8132 back to regs_t8140; T8122 path wedged m1n1 on j773g -- T8122 branch tripped SError
6b277bc pcie: apply apcie-phy-ip-{pll,auspma}-tunables on T8140 (fixes t8132 LTSSM stuck at BUSY)
```

Verbatim body of `6b277bc` (most recent):

> On j773g (base M4 / t8132) both active ports finish port bring-up with LINKSTS still asserting BUSY (0x8300020c / 0x83000204), meaning the port controller is running but LTSSM training never converges. Comparing the runtime dump to the source shows PHY digital brought up cleanly (PHYCMN_CLK=0x80300001 with the 100 MHz bit set, PHY_CTRL CLK0/CLK1 ACKs back), but the per-lane analog blocks were never tuned: the ADT under `/arm-io/apcie` carries apcie-phy-ip-pll-tunables and apcie-phy-ip-auspma-tunables, yet the PHY IP tunable application block at `pcie_init_controller()` was gated to T81XX / T602X / T8122 only. T8140 (and by extension the t8132 clause which reuses `regs_t8140`) silently skipped both properties.
> Add `APCIE_T8140` to the gate so those tunables get applied.
> Prior attempt to switch t8132 to `regs_t8122` wholesale wedged m1n1 with an SError because it dragged in T8122-only PHY register writes on top of the tunables.

Verbatim body of `dc25f2f`:

> Empirical result: switching t8132 to `regs_t8122` wedges m1n1 with no console output past 'Initializing t8132 PCIe controller' — the compat==T8122 branch applies apcie-phy-ip-{pll,auspma}-tunables plus extra PHY writes that trip an SError, and the exception handler stops making forward progress. The +0x8000 / +0x4000 PHY offset fix (line 388) is already applied for type==T8140 as well, so `regs_t8140` keeps the correct PHY addressing without the wedge-inducing extras.

**Reconciliation of the "phy_ip tunables work on the C side" claim:** the C-side runs full-speed CPU writes with ns-level intervals. The Python replay does one proxy round-trip per write (~ms). It's plausible that the C-side's phy_ip writes silently drop (no fault raised) but the code kept running with no readback — so the reader wouldn't notice. The final observed failure (LTSSM stuck at BUSY) is exactly what you'd see if the analog PLL was never calibrated (no lock → no LTSSM training convergence). This is consistent with our Python-side observation that phy_ip access is blocked in the read direction and possibly the write direction too. See RUN 3 candidate #2.

Also confirmed by inspection: `m1n1/src/pmgr.c:151-194` `pmgr_set_mode_recursive` walks parents-first via `pmgr_adt_get_parents` — the parent walk IS transitive. So when the ADT's `/arm-io/apcie` `clock-gates = [379, 380, 381, 382, 383, 151]` (gate 151 = `APCIE_PHY_SW`), calling `pmgr_adt_power_enable("/arm-io/apcie")` will drive gate 151 → its parents `APCIE_SYS_ST` and `APCIE_SYS_GP` → their parents, all to ACTIVE. Our runtime log Phase 0 shows `APCIE_SYS_ST actual=0x0 (OFF)` before Phase D poke — meaning m1n1's PMGR walk during the C-side `pcie_init()` probably ALSO left `APCIE_SYS_ST` and `APCIE_PHY_SW` off (since it never ran on our machine successfully). That's why perstn.py's Phase D exists as a separate step: it does what the C-side would have if it had matched a compat.

## 7. lore.kernel.org and Asahi wiki

**Not directly accessible via WebFetch.** `lore.kernel.org` is behind Anubis (Cloudflare-style proof-of-work challenge for anti-bot). GitHub-side commit history for `pcie-apple.c` and `drivers/phy/apple/` on the `asahi-soc/for-next` branch was reachable and matches `asahi-wip` — no additional t8132 patches pending.

From the user-provided lore.kernel.org front-page snapshot (`/tmp/page.html`), no thread mentions t8132 PCIe or PHY. The seven t8132 series patches on lore in 2026-07 are all DT-only:

- `[PATCH 02/10] dt-bindings: interrupt-controller: apple,aic2: Add apple,t8132 compatible`
- `[PATCH 03/10] dt-bindings: watchdog: apple,wdt: Add t8132 compatible`
- `[PATCH 04/10] dt-bindings: arm: apple: apple,pmgr: ...`
- `[PATCH 05/10] dt-bindings: power: apple,pmgr-pwrstate: ...`
- `[PATCH 08/10] dt-bindings: pwm: apple,s5l-fpwm: ...`
- `[PATCH 09/10] dt-bindings: arm: apple: Add M4 based devices`
- `[PATCH 10/10] arm64: dts: apple: Add minimal t8132 (M4) device trees`

That's it. **No PCIe, no PHY, no IOMMU, no NVMe patches for t8132 have ever been posted.** The bring-up of PCIe on M4 is not upstream work in progress — it's a hole.

## 8. Ranked RUN 3 hypothesis candidates (executed after RUN 2 result lands)

**Interpretation frame.** The atc.c/PCIe-atomic analog PLL analogy is the biggest new piece of evidence. If it's right, `phy_ip+0x38` = CIO3PLL_DCO_NCTRL analog, wedges because the CIO3PLL clock (analog of `atc.c:1778-1779` PCLK_EN + REFCLK_EN) has never been enabled. The 7-entry `apcie-cio3pllcore-tunables` presumably contains PLL calibration parameters analogous to atc.c's common tunable + DCO_NCTRL block. And its "clock enable" step is presumably encoded either in the pcieclkgen tunable or in some downstream port-init write we haven't done yet.

Candidates ranked by (falsifiability × informativeness × cheapness to run):

### 8.1  RUN 3a — cio3pllcore-naked-apply-to=phy_common_base (or rc_base)

**Change vs RUN 2:** RUN 2 targeted `axi_sub5_base` (address 0x495046200) with the 7-entry apply. If RUN 2 wedges, redo with different targets:

- Try **phy_common_base** (0x497004000) — analog of atc.c's `regs.core`. This is the strongest candidate.
- Try **rc_base** (0x494000000) — nominal `reg_idx=1` target the m1n1 broken applicator was aimed at.
- Try **phy_shared_base** (`phy_common_base + 0x4000` per pcie.c:395) — where the CLK 0/1 handshake and phy_shared+4=0x01 marker went.

Rationale: `atc.c:882-884` applies `common[0]` + `axi2af` + `common[1]` all to `regs.core` (not to a separate sub-block). By analogy the `apcie-cio3pllcore-tunables` writes belong in the "core" region of the PCIe PHY analog — which is `phy_common` in our recon geometry. RUN R's SKIP-NOOP result on axi_sub5 for entries #0-#3 (`0x00040a01`, `0x800`, `0xb00`, `0x0`) does NOT confirm sub5 is right — those pre-values could just be residual iBoot state on a completely different block.

**Wedge-immune diag:** `--phy-ip-diag-at=none`, widen reachable-scan windows to `phy_common+0..+0x2c00` (include the whole potential CIO3PLL range at +0x2a00), snapshot pre vs post 5.8.c. If any of entries #0-#6 land STUCK on phy_common but SKIP-NOOP on rc_base and axi_sub5, that identifies the target block. RUN 3a can never fail — even if step 6.g still wedges, the differential data is the deliverable.

Source citations: `atc.c:882-884, 1778-1779`, `pcie.c:452-457, 465-467`, `logs/1/nic-runtime.txt:399-455`.

### 8.2  RUN 3b — phy_ip write-only naked replay (findings.md candidate A, strengthened)

**Change vs RUN 2:** at step 6.g, replace `p.mask32(phy_ip_base + entry.offset, entry.mask, entry.value)` with a plain `p.write32(phy_ip_base + entry.offset, entry.value)` — no readback, no RMW. `atc.c` and the tunable library use `writel()` after the RMW, but if the read half stalls we can just eat the mask.

Rationale: Linux `pcie-apple.c` never reads `phy_ip`. The whole PHY analog window is programmed by the boot chain and never touched again by the driver. Symmetrically, if `phy_ip` is decode-locked in the READ direction only, a posted write may still land.

**Wedge-immune diag:** `--phy-ip-diag-at=none` throughout. Followed by reachable-scan of `port_phy_base` for any signal that phy_ip writes have propagated (LTSSM status bits, port_status). If port_status transitions from 0 to `PORT_STATUS_READY` (or LINKSTS_BUSY) later on, we know phy_ip writes are landing. Binary result: LANDED opens a whole "post writes only" strategy for the tunable applicator; STALL forces search back to Section 8.1 target-block bisection.

Source citations: `pcie-apple.c:508-542` (driver never reads phy_ip), `logs/1/findings.md:81` (Candidate A).

### 8.3  RUN 3c — T8122 shared-init post writes (findings.md candidate D)

**Change vs RUN 2:** apply the T8122-only writes T8140 skips, at `pcie.c:543-551`:

- `poll32(state->phy_base[0] + 0x8, 1, 1, 250000)` — wait for PHY clock enable on phy_shared+8
- `set32(state->phy_base[phy] + 0x0, 0x200)` — write bit 9 to phy_shared+0 (T8122 branch)

between step 6.f (T8140 marker at phy_shared+4=0x01) and step 6.g (first phy_ip write). The `dc25f2f` commit said the compat==T8122 branch that includes these tripped an SError. But that was on the C-side, on the WHOLE T8122 branch (which includes other writes). In a controlled python replay, we can wrap each write in `guarded()` and observe the fault independently.

Rationale: `phy_shared+0x8` bit 0 = "PHY clock ready" signal. If it's not being polled on t8132, we may proceed past step 6.f before the phy_shared clock domain is actually alive — and phy_ip access then goes into an unclocked block. This is the T8122-shape hypothesis: t8132 needs some subset of the T8122 writes T8140 lets through.

**Wedge-immune diag:** poll32 is guarded already. Individual entry+exit flushes before and after each write. Reachable-scan `phy_shared+0..+0x20` before + after each.

Source citations: `pcie.c:539-554`, `logs/1/findings.md:84` (Candidate D), `dc25f2f` commit body ("compat==T8122 branch ... trip an SError").

### 8.4  RUN 3d — atc.c-style CIO3PLL enable pair before phy_ip

**Change vs RUN 2:** after step 6.f but before 6.g:

- `p.set32(<target_base> + 0x2a00, BIT(1))`  # PCLK_EN — atc.c:1778 analog
- `p.set32(<target_base> + 0x2a00, BIT(5))`  # REFCLK_EN — atc.c:1779 analog

`<target_base>` chosen from RUN 3a's differential result. If RUN 3a's cio3pllcore lands on phy_common, use `phy_common_base`. Otherwise use whatever block RUN 3a identified.

Rationale: this is the direct atc.c-analog "turn on the PLL clock" step. On atc.c it comes AFTER apply_tunables + FSM kick + all the CFG0/SLEEP_CTRL overrides. The overrides (SMALL/BIG/CLAMP for TX and RX) might or might not have direct PCIe analogs — but the PCLK/REFCLK enable step is the most obviously reusable.

**Wedge-immune diag:** guarded set32, exc_count delta check, phy_common reachable-scan pre/post. If the set32 raises SYNC we've hit a live register in the CIO3PLL block for the first time — highly informative.

Source citations: `atc.c:112-114, 1778-1779`.

### 8.5  Deferred diagnostics from findings.md (B, C)

These are lower-priority follow-ups that clarify the axi_base bit-31 clear window and the axi_base+0x38/+0x40 write-lock question but don't by themselves unblock phy_ip. Keep them queued.

- Candidate B — mid-apply reachable-scan hook to bisect the axi_base+0 bit 31 clear
- Candidate C — axi_base+0x38 / +0x40 write-lock probe with progressively wider masks

Source: `logs/1/findings.md:82-83`.

### 8.6  Priority ordering

Recommended dispatch order for RUN 3+:

1. **RUN 3a** (differential cio3pllcore target, wedge-immune) — always safe to run first; identifies the block.
2. **RUN 3b** (phy_ip write-only) — binary result, hard-forks the rest.
3. **RUN 3d** (atc.c CIO3PLL enable pair) — depends on 3a's block identification.
4. **RUN 3c** (T8122 shared-init post) — last un-tried T8122/T81XX write-set.
5. **RUN 3 candidates B/C** — diagnostic-only, keep queued.

## 9. Open questions no source answers

1. What target ADT block does `apcie-cio3pllcore-tunables` actually address? Only iBoot RE would say for sure. Our best inference is `phy_common_base` (the "core" analog of atc.c's `regs.core`), but this is empirical.
2. What programs the CIO3PLL on macOS boots? Possibly a mach-o kext (`ApplePCIEPortNoRebootStub` / `AppleH13PCIePort` family) not published. Alternative: an ANE/AOP-side firmware payload that runs before macOS scheduler starts.
3. Why does the m1n1 C-side get past `phy_ip+0x38` writes (per commit `6b277bc` reaching LTSSM BUSY) but the Python replay stalls at the same offset? Timing? Silent write-drop with no fault? Barrier / DSB / ISB ordering the compiler emits that we don't? Unknown.
4. Is `axi_base+0x38 / +0x40`'s write-lock (RUN S/1 finding) correlated with `phy_ip+0x38`'s stall? The offset alignment is suspicious but unproven.
5. On t8132, `ps_apcie_phy_sw` requires both `sys_st` and `sys_gp` chains. If macOS explicitly manages this gate (unlike t8122 always-on), there's likely a corresponding runtime power-management sequence that iBoot invokes before handoff. We satisfy the gate-open condition but not necessarily any handshake macOS does simultaneously (e.g., a "wait for PLL lock" ack).

## 10. Cross-links

- [[project-m4-pcie-bringup]] — the multi-run bring-up log this reference supports
- [[ref-m4-repos]] — repo paths, script names, log locations
