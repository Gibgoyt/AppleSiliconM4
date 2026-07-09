# Phase 1 recon summary

Auto-generated draft. Read the referenced files under this directory for detail;
fill in the 'Notes' section by hand.

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

## Q2 — NIC PCIe VID:DID + BAR0

_Deferred to Phase 3._ VID:DID and BAR0 come from the NIC's PCIe
config space header, which is only readable after `pcie_init` has
actually powered and trained the root complex the NIC sits behind.
On T8132 that requires the `apcie,t8132` compat clause to be
present in m1n1's `src/pcie.c`. Until then, the ADT already tells
us the ADT-side identity of the device (grep `adt.txt` for the
nodes flagged in the Notes section below).

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

## Q4 — DART node candidates for the NIC

    dart-apcie2	compatible=ListContainer(['dart,t8110'])
    dart-apcie0	compatible=ListContainer(['dart,t8110'])
    dart-apciec0	compatible=ListContainer(['dart,t8110'])
    dart-apciec1	compatible=ListContainer(['dart,t8110'])
    dart-apciec3	compatible=ListContainer(['dart,t8110'])

See `dart-<name>.txt` for L1/L2 dumps of each apcie DART.

## Notes / surprises

_Fill in after inspecting the files above._

## Files produced

- `adt.bin` (419392 bytes)
- `adt.txt` (963704 bytes)
- `arm-io-children.txt` (1309 bytes)
- `darts.txt` (1505 bytes)
- `mac-address.txt` (477 bytes)
- `pcie-config-space-apcie.txt` (43 bytes)
- `pcie-init.txt` (19 bytes)
- `pcie-nodes.txt` (6181 bytes)
- `smc-power.txt` (83 bytes)
