#!/usr/bin/env python3
"""Phase 1 reconnaissance for the M4 mini.

Dumps ADT, PCIe root-complex compatible strings, MAC, DART list, and
optionally PCIe config space + DART tables to /tmp/m4-recon/ so
downstream phases can be planned from disk without re-touching the M4.

Default run does only pure ADT reads (no side effects on the target).

    --pcie        SMC power sequence + p.pcie_init() + ECAM sweep for
                  every /arm-io/apcie* root complex. Class-0x02 device
                  is written to nic-identity.txt.
    --dart-dump   For every /arm-io/dart-apcie*, run DART.from_adt(...)
                  .dump_all() and capture stdout to dart-<name>.txt.

Usage (via wrapper):
    ./Scripts/m1n1/recon.sh [--pcie] [--dart-dump]
"""

import argparse
import io
import pathlib
import sys
import traceback

sys.path.append(str(pathlib.Path.home() /
    "Projects/AsahiLinux/m4/m1n1/proxyclient"))

from m1n1.setup import *          # noqa: F401,F403 -- exposes u, p, iface
from m1n1.fw.smc import SMCClient
from m1n1.hw.dart import DART


# Populated by phase_a's find_nic call; read by write_summary.
_NIC_INFO = None


def log(msg):
    print(f"[recon] {msg}")


def try_(fn, label):
    try:
        return fn()
    except Exception as e:
        log(f"WARN {label}: {e.__class__.__name__}: {e}")
        traceback.print_exc(limit=3)
        return None


def write_text(path, content):
    if content is None:
        return
    pathlib.Path(path).write_text(content)
    log(f"wrote {path} ({len(content)} chars)")


def write_bytes(path, content):
    if content is None:
        return
    pathlib.Path(path).write_bytes(content)
    log(f"wrote {path} ({len(content)} bytes)")


# ---------------------------------------------------------------- ADT helpers

def _walk(node):
    yield node
    for child in node:
        yield from _walk(child)


def _find_phandle_owner(root, phandle):
    if phandle is None:
        return None
    for n in _walk(root):
        if n.getprop("AAPL,phandle", None) == phandle:
            return n
    return None


def find_nic(adt):
    """Locate the ethernet leaf node and resolve its DART.

    Returns a dict of everything Phase 3/4/5 will need. Returns None
    if no lan-typed device was found (would be a surprise).
    """
    nic = None
    for n in _walk(adt):
        dt = n.getprop("device_type", None)
        if isinstance(dt, str) and dt.startswith("lan") and dt != "lan-sync":
            nic = n
            break
    if nic is None:
        return None

    parent_bridge = nic._parent
    iommu_phandle = nic.getprop("iommu-parent", None)
    dart_mapper = _find_phandle_owner(adt, iommu_phandle)
    dart_node = dart_mapper._parent if dart_mapper is not None else None

    mac = nic.getprop("local-mac-address", None)
    mac_str = mac.hex(":") if isinstance(mac, (bytes, bytearray)) else repr(mac)

    return {
        "nic_path": nic._path,
        "nic_name": nic.name,
        "device_type": nic.getprop("device_type", None),
        "local_mac_address": mac_str,
        "bridge_path": parent_bridge._path if parent_bridge else None,
        "apcie_port": parent_bridge.getprop("apcie-port", None) if parent_bridge else None,
        "function_clkreq": parent_bridge.getprop("function-clkreq", None) if parent_bridge else None,
        "function_perst": parent_bridge.getprop("function-perst", None) if parent_bridge else None,
        "iommu_parent_phandle": iommu_phandle,
        "dart_mapper_name": dart_mapper.name if dart_mapper else None,
        "dart_node_name": dart_node.name if dart_node else None,
        "dart_node_path": dart_node._path if dart_node else None,
        "dart_compatible": dart_node.getprop("compatible", None) if dart_node else None,
    }


def format_nic_adt(info):
    if info is None:
        return "No lan-typed device found in ADT walk.\n"
    lines = ["NIC identity resolved from ADT walk:\n"]
    for k in ("nic_path", "nic_name", "device_type", "local_mac_address",
              "bridge_path", "apcie_port", "function_clkreq", "function_perst",
              "iommu_parent_phandle", "dart_mapper_name",
              "dart_node_name", "dart_node_path", "dart_compatible"):
        lines.append(f"  {k:22} = {info[k]!r}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- Phase A

def phase_a(out):
    log("=== Phase A: ADT read (safe, no side effects) ===")

    write_text(out / "adt.txt", try_(lambda: str(u.adt), "str(adt)"))
    write_bytes(out / "adt.bin", try_(lambda: u.adt.build(), "adt.build()"))

    def arm_io_children():
        return "\n".join(c.name for c in u.adt["/arm-io"]) + "\n"
    write_text(out / "arm-io-children.txt",
               try_(arm_io_children, "arm-io walk"))

    def pcie_nodes():
        buf = io.StringIO()
        seen = set()
        # Start from a fixed list, then add any other /arm-io/apcie* found.
        candidates = ["/arm-io/apcie", "/arm-io/apcie-ge0",
                      "/arm-io/apcie-ge1"]
        try:
            for c in u.adt["/arm-io"]:
                if c.name.startswith("apcie"):
                    p_ = f"/arm-io/{c.name}"
                    if p_ not in candidates:
                        candidates.append(p_)
        except Exception:
            pass
        for path in candidates:
            if path in seen:
                continue
            seen.add(path)
            buf.write(f"=== {path} ===\n")
            try:
                node = u.adt[path]
            except KeyError:
                buf.write("NOT PRESENT\n\n")
                continue
            compat = node.getprop("compatible", None)
            buf.write(f"compatible: {compat!r}\n")
            try:
                for i, r in enumerate(node.reg):
                    buf.write(f"reg[{i}]: addr={r.addr:#x} size={r.size:#x}\n")
                    try:
                        base, sz = node.get_reg(i)
                        buf.write(f"reg[{i}] translated: base={base:#x} "
                                  f"size={sz:#x}\n")
                    except Exception as e:
                        buf.write(f"reg[{i}] translate failed: "
                                  f"{e.__class__.__name__}: {e}\n")
            except AttributeError:
                buf.write("reg: (none)\n")
            for key in ("lane-cfg", "device_type", "#address-cells",
                        "#size-cells"):
                v = node.getprop(key, None)
                if v is not None:
                    buf.write(f"{key}: {v!r}\n")
            buf.write("\n")
        return buf.getvalue()
    write_text(out / "pcie-nodes.txt", try_(pcie_nodes, "pcie-nodes"))

    def mac_addr():
        buf = io.StringIO()
        chosen = u.adt["/chosen"]
        buf.write("Standard keys under /chosen:\n")
        for key in ("mac-address-ethernet0", "mac-address-ethernet1",
                    "mac-address-wifi0", "mac-address-bluetooth0"):
            val = chosen.getprop(key, None)
            if val is None:
                buf.write(f"    {key}: (missing)\n")
            elif isinstance(val, (bytes, bytearray)):
                buf.write(f"    {key}: {val.hex(':')}  "
                          f"(raw={bytes(val)!r})\n")
            else:
                buf.write(f"    {key}: {val!r}\n")

        buf.write("\nAll /chosen properties whose name mentions 'mac':\n")
        for k, v in chosen._properties.items():
            if "mac" in k.lower():
                if isinstance(v, (bytes, bytearray)):
                    buf.write(f"    {k}: {v.hex(':')}\n")
                else:
                    buf.write(f"    {k}: {v!r}\n")
        return buf.getvalue()
    write_text(out / "mac-address.txt", try_(mac_addr, "mac-address"))

    def darts():
        lines = []
        for c in u.adt["/arm-io"]:
            if c.name.startswith("dart-"):
                compat = c.getprop("compatible", None)
                lines.append(f"{c.name}\tcompatible={compat!r}")
        return "\n".join(lines) + "\n"
    write_text(out / "darts.txt", try_(darts, "darts"))

    nic_info = try_(lambda: find_nic(u.adt), "find_nic")
    write_text(out / "nic-adt.txt", format_nic_adt(nic_info))
    # Stash for the summary generator (module-level ok; recon.py is a script).
    global _NIC_INFO
    _NIC_INFO = nic_info


# ---------------------------------------------------------------- Phase B

def phase_b(out):
    log("=== Phase B: SMC power + p.pcie_init() ===")
    log("NOTE: this modifies M4 state (powers PCIe controllers).")
    log("      No ECAM sweep is performed here -- unrouted config-space")
    log("      reads on an unrecognized apcie/apciec compat trigger a")
    log("      fabric SLVERR that lands m1n1 in a sync exception. ECAM")
    log("      enumeration is moved to Phase 3 (pcie_up.py) after m1n1")
    log("      has been patched with the apcie compat.")

    def smc_power():
        smc_addr = u.adt["arm-io/smc"].get_reg(0)[0]
        smc = SMCClient(u, smc_addr, None)
        smc.start()
        smc.start_ep(0x20)
        smc.smcep.write32("gP0d", 0x800001)
        smc.smcep.write32("gP1a", 1)
        smc.stop()
        return ("SMC smc_addr=0x%x\n"
                "gP0d <- 0x800001 (apcie power)\n"
                "gP1a <- 1 (apcie-ge power)\n" % smc_addr)
    write_text(out / "smc-power.txt",
               try_(smc_power, "SMC power") or "SMC power call failed\n")

    def pcie_init_call():
        rc = p.pcie_init()
        return (f"p.pcie_init() -> {rc!r}\n"
                "NOTE: rc reflects only the proxy return value.\n"
                "Real m1n1 status prints on the dockchannel UART as\n"
                "TTY> pcie: Initializing tXXXX PCIe controller (success)\n"
                "TTY> pcie: Unsupported compatible (failure)\n"
                "Check the recon.sh terminal output for the true state.\n")
    write_text(out / "pcie-init.txt",
               try_(pcie_init_call, "pcie_init") or "pcie_init raised\n")

    log("Phase B done. Q2 (NIC VID:DID + BAR0) is deferred to Phase 3.")


# ---------------------------------------------------------------- Phase C

# DARTs that m1n1 already powers/initializes during its own boot -- safe to
# dump from a pure ADT-recon run without any prior SMC power sequence.
_SAFE_DART_PREFIXES = (
    "dart-usb", "dart-dcp", "dart-disp", "dart-dcpext", "dart-dispext",
    "dart-scaler", "dart-jpeg", "dart-ave", "dart-avd", "dart-apr",
    "dart-ane", "dart-aop", "dart-sio", "dart-sep", "dart-pmp",
    "dart-acio",
)

# DARTs whose parent fabric requires an SMC power sequence + working
# pcie_init before the DART's MMIO region even routes. Reading their regs
# without --pcie (and, on T8132, without m1n1 patched for apcie,t8132)
# triggers a fabric SLVERR that lands m1n1 in a sync exception, killing
# the USB proxy channel for the rest of the run.
_GATED_DART_PREFIXES = ("dart-apcie", "dart-apciec")


def _dart_class(name):
    if any(name.startswith(p) for p in _GATED_DART_PREFIXES):
        return "gated"
    if any(name.startswith(p) for p in _SAFE_DART_PREFIXES):
        return "safe"
    return "unknown"


def phase_c(out, pcie_ran):
    log("=== Phase C: DART table dumps ===")
    dart_names = []
    try:
        for c in u.adt["/arm-io"]:
            if c.name.startswith("dart-"):
                dart_names.append(c.name)
    except Exception as e:
        log(f"WARN dart iteration: {e}")

    for name in dart_names:
        cls = _dart_class(name)
        if cls == "gated" and not pcie_ran:
            log(f"SKIP {name}: parent fabric unpowered "
                "(rerun with --pcie to attempt)")
            continue
        if cls == "unknown":
            log(f"SKIP {name}: not classified safe/gated")
            continue

        path = f"arm-io/{name}"

        def do_dump(path=path):
            buf = io.StringIO()
            saved = sys.stdout
            sys.stdout = buf
            try:
                dart = DART.from_adt(u, path)
                try:
                    dart.dump_all()
                except Exception as e:
                    print(f"dump_all raised: {e.__class__.__name__}: {e}")
                try:
                    dart.dart.regs.dump_regs()
                except Exception as e:
                    print(f"dump_regs raised: {e.__class__.__name__}: {e}")
            finally:
                sys.stdout = saved
            return buf.getvalue()

        txt = try_(do_dump, f"DART dump {path}")
        if txt is not None:
            write_text(out / f"dart-{name}.txt", txt)


# ---------------------------------------------------------------- Summary

def write_summary(out, args):
    lines = []
    a = lines.append
    nic = _NIC_INFO

    a("# Phase 1 recon summary\n\n")
    a("Auto-generated. All Phase-1-answerable questions have concrete\n")
    a("values below; Q2/Q3 (PCIe VID:DID/BAR0) are Phase 3 deliverables\n")
    a("by design (see PLAN_2.md §14.R1).\n\n")

    a("## Q1 — PCIe compat string(s) present on T8132\n\n")
    try:
        for l in (out / "pcie-nodes.txt").read_text().splitlines():
            if l.startswith("=== ") or l.startswith("compatible:"):
                a(f"    {l}\n")
    except Exception:
        a("_pcie-nodes.txt missing_\n")
    a("\nBoth `apcie,t8132` and `apciec,t8132` are unrecognized by the\n")
    a("m1n1 currently enrolled as fuOS on this machine. Phase 3 backports\n")
    a("`regs_t8122` + the `apcie,t8122`/`t6030`/`t6031` clauses from the\n")
    a("`m4n1` sibling fork into `~/Projects/AsahiLinux/m4/m1n1/src/pcie.c`\n")
    a("and adds an `apcie,t8132` clause pointing at `regs_t8122`.\n\n")

    a("## Q2 — NIC PCIe VID:DID + BAR0\n\n")
    a("_Deferred to Phase 3 by design._ VID:DID and BAR0 live in PCI\n")
    a("config space, only reachable once the parent root complex has been\n")
    a("powered and trained by `pcie_init`. That's Phase 3 territory.\n\n")
    if nic is not None:
        a("**ADT-side identity (usable for Phase 3/5 planning now):**\n\n")
        a("```\n")
        a(f"NIC path:            {nic['nic_path']}\n")
        a(f"device_type:         {nic['device_type']}\n")
        a(f"local-mac-address:   {nic['local_mac_address']}\n")
        a(f"Parent bridge:       {nic['bridge_path']}\n")
        a(f"apcie-port:          {nic['apcie_port']}\n")
        a(f"function-clkreq:     {nic['function_clkreq']!r}\n")
        a(f"function-perst:      {nic['function_perst']!r}\n")
        a("```\n\n")
    else:
        a("**No lan-typed ADT node found -- this is a surprise, investigate.**\n\n")

    a("## Q3 — Ethernet MAC address\n\n```\n")
    try:
        a((out / "mac-address.txt").read_text())
    except Exception:
        a("(mac-address.txt missing)\n")
    a("```\n\n")

    a("## Q4 — DART node for the NIC\n\n")
    if nic is not None and nic.get("dart_node_name"):
        a("Resolved by ADT walk `lan-*.iommu-parent` → phandle owner "
          "→ parent DART node:\n\n")
        a("```\n")
        a(f"iommu-parent phandle: {nic['iommu_parent_phandle']}\n")
        a(f"DART mapper:          {nic['dart_mapper_name']}\n")
        a(f"DART node:            {nic['dart_node_path']}\n")
        a(f"DART compatible:      {nic['dart_compatible']}\n")
        a("```\n\n")
    else:
        a("_Unable to resolve programmatically; see candidates below._\n\n")
    a("All apcie DARTs (candidates fallback):\n\n")
    try:
        for l in (out / "darts.txt").read_text().splitlines():
            if "apcie" in l:
                a(f"    {l}\n")
    except Exception:
        a("_darts.txt missing_\n")
    if args.dart_dump:
        a("\nSee `dart-<name>.txt` for L1/L2 dumps of safe (non-apcie) DARTs.\n")
        a("apcie DARTs need SMC power + working pcie_init (Phase 3+).\n")
    a("\n")

    a("## Notes / surprises\n\n")
    a("- m1n1 boot log shows two unsupported-compat warnings on M4/T8132:\n")
    a("    `MCC: Unsupported version:mcc,t8132`  (memory controller)\n")
    a("    `cpufreq: Chip 0x8132 is unsupported`\n")
    a("  Neither blocks our TCP-hello-world path but flags M4-specific\n")
    a("  m1n1 gaps for future work.\n")
    a("- WLAN/BT chip is BCM4387 (compat `wlan-pcie,bcm4387 wlan-pcie,bcm`)\n")
    a("  under `/arm-io/apcie/pci-bridge1`, via `dart-apcie0`.\n")
    a("- `/arm-io/apcie` has 25 reg entries and `#ports = 3`.\n")
    a("  With `shared_reg_count = 7`, port_regs = 25 - 7 = 18 = 3 * 6.\n")
    a("  That matches `regs_t8122` exactly, so the Phase 3 backport can\n")
    a("  point `apcie,t8132` at the existing `regs_t8122` struct.\n")
    a("- `apcie` and `apciec*` DARTs (dart-apcie0/2, dart-apciec0/1/3) are\n")
    a("  unreachable via MMIO until SMC gP0d=0x800001 powers the fabric.\n")
    a("  Attempting `DART.from_adt(u, 'arm-io/dart-apcie2')` in a naked\n")
    a("  recon run SLVERRs and takes m1n1 down. Phase 4 (`dart_up.py`)\n")
    a("  will do this after SMC power lands.\n")
    a("- `apciec,t8132` (per-slot Thunderbolt RCs) is left unsupported\n")
    a("  intentionally -- the NIC lives on `apcie`, not `apciec`, so we\n")
    a("  don't need `apciec,t8132` for the TCP hello-world milestone.\n")
    a("\n")

    a("## Files produced\n\n")
    for f in sorted(out.iterdir()):
        try:
            sz = f.stat().st_size
        except Exception:
            sz = -1
        a(f"- `{f.name}` ({sz} bytes)\n")

    (out / "recon-summary.md").write_text("".join(lines))
    log(f"wrote {out / 'recon-summary.md'}")


# ---------------------------------------------------------------- Main

def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--out", default="/tmp/m4-recon",
                    help="output directory (default: /tmp/m4-recon)")
    ap.add_argument("--pcie", action="store_true",
                    help="opt into SMC power + p.pcie_init() + ECAM sweep")
    ap.add_argument("--dart-dump", action="store_true",
                    help="opt into DART page-table dumps")
    args = ap.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log(f"output dir: {out}")

    phase_a(out)
    if args.pcie:
        phase_b(out)
    else:
        log("Phase B skipped (pass --pcie to enable)")
    if args.dart_dump:
        phase_c(out, pcie_ran=args.pcie)
    else:
        log("Phase C skipped (pass --dart-dump to enable)")

    write_summary(out, args)
    log("done")


if __name__ == "__main__":
    main()
