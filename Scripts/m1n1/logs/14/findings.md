# RUN 14 findings — THE BREAKTHROUGH RUN

**Dispatch:** `./Scripts/m1n1/perstn-run.sh 14` (dispatcher `1154aba`) on
m1n1 `8a569ad` (t8132 phy-ip tunables skip + PCIE_BC flushed breadcrumbs).
Banner `v1.6.0-rc1-59-g8a569ad` (`run.log:38`); `require-build OK:
'm1n1 uartproxy v1.6.0-rc1-59-g' contains 'rc1-59-g'` (`run.log:127`) —
both new guards worked.

**Result: `p.pcie_init() -> 0`** — the first completed C-side init since
2026-07-10, ending the 6b277bc ordering-bug era. Full breadcrumb trail
(`run.log:195-212`):

    pcie: Initializing t8132 PCIe controller
    pcie: ADT uses 6 reg entries per port
    pcie: BC pmgr power enable... / done
    pcie: BC axi2af tunables done
    pcie: No common tunables / BC common tunables done
    pcie: BC phy tunables done
    pcie: BC phy 0 CLK handshake done
    pcie: t8132: skipping phy-ip tunables pre-port-init (host applies post-init)
    pcie: BC shared init done (rc handshake ok)
    pcie: Initializing port 0
    pcie: Port failed to become idle on /arm-io/apcie/pci-bridge0
    pcie: Initializing port 2
    pcie: Port failed to become idle on /arm-io/apcie/pci-bridge2
    pcie: Initialized controller 0

The wedge then moved to the post-init step: `[flush:postinit-phyip.pre-pll]`
is the last run.log line — the `p.tunables_apply_local` pll apply hit the
still-locked phy_ip (`0x497040038`) and the session died.

## Finding 1: ports-stuck-BUSY is pcie_up_1 PARITY, not a regression

The idle check (pcie.c:866-870): `poll32(port_base+0x208 LINKSTS,
BUSY=BIT(2), want 0, 250ms)` → on timeout print + `continue` (since m1n1
`7a1a083`; was `return -1` before — which is why early runs never got past
a stuck port). The `continue` skips the rest of the port body (RC/DWC
config, LTSSM setup, MSI map, the final LINKSTS breadcrumb).

**pcie_up_1's post-init LINKSTS reads were `0x8300020c` (port 0) /
`0x83000204` (port 2) — BUSY (bit 2) SET.** The known-good 2026-07-10 boot
had the same ports-stuck-BUSY outcome (its pcie: console lines were
buffered/unlogged in that era). RUN 14 reproduces pcie_up_1 faithfully,
port behavior included. Port idle/LTSSM is a later problem, tied to
CLKREQ#/refclk/endpoint state — not the current blocker.

## Finding 2: phy_ip is still locked right after pcie_init — and what 2026-07-11 did differently

On 2026-07-11 (the only boot where phy_ip ever decoded), ports were ALSO
stuck at LINKSTS_BUSY ("After p.pcie_init() has returned (with ports
stuck at LINKSTS_BUSY)…") — so **per-port completion is NOT the ungate**.
What that boot did between `pcie_init` and its successful 6.g: **it
re-ran the entire Phase F shared sequence** — pmgr enable, axi2af
tunables, phy tunables, a SECOND CLK0REQ/ACK + CLK1REQ/ACK handshake on
phy_shared, RESET clear, T8140 marker — and only then applied the pll
tunables (all 29 landed). RUN 14 went straight from `pcie_init` to the
pll apply and wedged.

**Corrected hypothesis: the phy_ip ungate is the post-init RE-PASS of the
shared handshake sequence** (most plausibly the second CLK0/CLK1
handshake from the post-init state).

## RUN 15 plan (no reflash — m1n1 8a569ad stays enrolled)

`--t8140-replay-post-init`: run `probe_phaseF_t8140_replay` (native
order, no experimental flags — exactly d664bd9's Phase F) AFTER
`pcie_init` returns. Phase F's 6.g applies the 29 pll entries per-entry
python-side; 6.h applies auspma with the port-1 slice filtered (the
2026-07-11 killer). Plus an early tier-1 dump right after `pcie_init`
(captures the port LINKSTS state RUN 14 never logged). Then tier-3 dumps
(Tier 3a phy_ip harvest), LTSSM kick, ECAM walk.

Matrix:
- **Phase F completes (6.g/6.h/7-10)** → tunables in, best state ever →
  harvest, kick, ECAM. NIC vendor/device ID = goal. Ports still BUSY
  after everything → RUN 16 = LTSSM/link-training work (CLKREQ#/refclk
  axis) from a fully-tuned controller.
- **6.g wedges even post-replay** → 2026-07-11 reproduced except m1n1
  binary internals → diff the d664bd9-era port-body pre-idle phy writes
  (`clear32(...0x10)` / `set32 0x200/0x400`, pcie.c:836-843) vs 8a569ad,
  or bisect the m1n1 commits between.
- **An earlier Phase F step wedges post-init** → the step label names it.
