# Phase 1 recon summary

Auto-generated. All Phase-1-answerable questions have concrete
values below; Q2/Q3 (PCIe VID:DID/BAR0) are Phase 3 deliverables
by design (see PLAN_2.md §14.R1).

## Q1 — PCIe compat string(s) present on T8132

    === /arm-io/apcie ===
    compatible: ListContainer(['apcie,t8132'])
    === /arm-io/apcie-ge0 ===
    === /arm-io/apcie-ge1 ===
    === /arm-io/apciec0 ===
    compatible: ListContainer(['apciec,t8132'])
    === /arm-io/apciec0-piodma ===
    compatible: ListContainer(['pciec-apiodma,t8103'])
    === /arm-io/apciec1 ===
    compatible: ListContainer(['apciec,t8132'])
    === /arm-io/apciec1-piodma ===
    compatible: ListContainer(['pciec-apiodma,t8103'])
    === /arm-io/apciec3 ===
    compatible: ListContainer(['apciec,t8132'])
    === /arm-io/apciec3-piodma ===
    compatible: ListContainer(['pciec-apiodma,t8103'])

Both `apcie,t8132` and `apciec,t8132` are unrecognized by the
m1n1 currently enrolled as fuOS on this machine. Phase 3 backports
`regs_t8122` + the `apcie,t8122`/`t6030`/`t6031` clauses from the
`m4n1` sibling fork into `~/Projects/AsahiLinux/m4/m1n1/src/pcie.c`
and adds an `apcie,t8132` clause pointing at `regs_t8122`.

## Q2 — NIC PCIe VID:DID + BAR0

_Deferred to Phase 3 by design._ VID:DID and BAR0 live in PCI
config space, only reachable once the parent root complex has been
powered and trained by `pcie_init`. That's Phase 3 territory.

**ADT-side identity (usable for Phase 3/5 planning now):**

```
NIC path:            /device-tree/arm-io/apcie/pci-bridge2/lan-1gb
device_type:         lan0
local-mac-address:   d0:11:e5:71:81:dc
Parent bridge:       /device-tree/arm-io/apcie/pci-bridge2
apcie-port:          2
function-clkreq:     Container(phandle=120, name=u'GPIO', args=ListContainer([162, 2]))
function-perst:      Container(phandle=120, name=u'GPIO', args=ListContainer([165, 0]))
```

## Q3 — Ethernet MAC address

```
Standard keys under /chosen:
    mac-address-ethernet0: d0:11:e5:71:81:dc  (raw=b'\xd0\x11\xe5q\x81\xdc')
    mac-address-ethernet1: (missing)
    mac-address-wifi0: d0:11:e5:88:15:f0  (raw=b'\xd0\x11\xe5\x88\x15\xf0')
    mac-address-bluetooth0: d0:11:e5:8b:3c:30  (raw=b'\xd0\x11\xe5\x8b<0')

All /chosen properties whose name mentions 'mac':
    mac-address-wifi0: d0:11:e5:88:15:f0
    mac-address-bluetooth0: d0:11:e5:8b:3c:30
    mac-address-ethernet0: d0:11:e5:71:81:dc
```

## Q4 — DART node for the NIC

Resolved by ADT walk `lan-*.iommu-parent` → phandle owner → parent DART node:

```
iommu-parent phandle: 49
DART mapper:          mapper-apcie2
DART node:            /device-tree/arm-io/dart-apcie2
DART compatible:      ListContainer: 
    dart,t8110
```

All apcie DARTs (candidates fallback):

    dart-apcie2	compatible=ListContainer(['dart,t8110'])
    dart-apcie0	compatible=ListContainer(['dart,t8110'])
    dart-apciec0	compatible=ListContainer(['dart,t8110'])
    dart-apciec1	compatible=ListContainer(['dart,t8110'])
    dart-apciec3	compatible=ListContainer(['dart,t8110'])

## Notes / surprises

- m1n1 boot log shows two unsupported-compat warnings on M4/T8132:
    `MCC: Unsupported version:mcc,t8132`  (memory controller)
    `cpufreq: Chip 0x8132 is unsupported`
  Neither blocks our TCP-hello-world path but flags M4-specific
  m1n1 gaps for future work.
- WLAN/BT chip is BCM4387 (compat `wlan-pcie,bcm4387 wlan-pcie,bcm`)
  under `/arm-io/apcie/pci-bridge1`, via `dart-apcie0`.
- `/arm-io/apcie` has 25 reg entries and `#ports = 3`.
  With `shared_reg_count = 7`, port_regs = 25 - 7 = 18 = 3 * 6.
  That matches `regs_t8122` exactly, so the Phase 3 backport can
  point `apcie,t8132` at the existing `regs_t8122` struct.
- `apcie` and `apciec*` DARTs (dart-apcie0/2, dart-apciec0/1/3) are
  unreachable via MMIO until SMC gP0d=0x800001 powers the fabric.
  Attempting `DART.from_adt(u, 'arm-io/dart-apcie2')` in a naked
  recon run SLVERRs and takes m1n1 down. Phase 4 (`dart_up.py`)
  will do this after SMC power lands.
- `apciec,t8132` (per-slot Thunderbolt RCs) is left unsupported
  intentionally -- the NIC lives on `apcie`, not `apciec`, so we
  don't need `apciec,t8132` for the TCP hello-world milestone.

## Files produced

- `adt.bin` (419392 bytes)
- `adt.txt` (963700 bytes)
- `arm-io-children.txt` (1309 bytes)
- `darts.txt` (1505 bytes)
- `mac-address.txt` (477 bytes)
- `nic-adt.txt` (747 bytes)
- `pcie-config-space-apcie.txt` (43 bytes)
- `pcie-init.txt` (19 bytes)
- `pcie-nodes.txt` (6181 bytes)
- `recon-summary.md` (2572 bytes)
- `smc-power.txt` (83 bytes)
