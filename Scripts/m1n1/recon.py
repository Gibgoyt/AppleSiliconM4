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

def phase_c(out):
    log("=== Phase C: DART table dumps ===")
    dart_names = []
    try:
        for c in u.adt["/arm-io"]:
            if c.name.startswith("dart-") and "apcie" in c.name:
                dart_names.append(c.name)
    except Exception as e:
        log(f"WARN dart iteration: {e}")

    for name in dart_names:
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

    a("# Phase 1 recon summary\n\n")
    a("Auto-generated draft. Read the referenced files under this "
      "directory for detail;\nfill in the 'Notes' section by hand.\n\n")

    a("## Q1 — PCIe compat string(s) present on T8132\n\n")
    try:
        for l in (out / "pcie-nodes.txt").read_text().splitlines():
            if l.startswith("=== ") or l.startswith("compatible:"):
                a(f"    {l}\n")
    except Exception:
        a("_pcie-nodes.txt missing_\n")
    a("\n")

    a("## Q2 — NIC PCIe VID:DID + BAR0\n\n")
    a("_Deferred to Phase 3._ VID:DID and BAR0 come from the NIC's PCIe\n")
    a("config space header, which is only readable after `pcie_init` has\n")
    a("actually powered and trained the root complex the NIC sits behind.\n")
    a("On T8132 that requires the `apcie,t8132` compat clause to be\n")
    a("present in m1n1's `src/pcie.c`. Until then, the ADT already tells\n")
    a("us the ADT-side identity of the device (grep `adt.txt` for the\n")
    a("nodes flagged in the Notes section below).\n")
    a("\n")

    a("## Q3 — Ethernet MAC address\n\n```\n")
    try:
        a((out / "mac-address.txt").read_text())
    except Exception:
        a("(mac-address.txt missing)\n")
    a("```\n\n")

    a("## Q4 — DART node candidates for the NIC\n\n")
    try:
        for l in (out / "darts.txt").read_text().splitlines():
            if "apcie" in l:
                a(f"    {l}\n")
    except Exception:
        a("_darts.txt missing_\n")
    if args.dart_dump:
        a("\nSee `dart-<name>.txt` for L1/L2 dumps of each apcie DART.\n")
    else:
        a("\n_TODO: re-run with --dart-dump to capture page tables._\n")
    a("\n")

    a("## Notes / surprises\n\n")
    a("_Fill in after inspecting the files above._\n\n")

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
        phase_c(out)
    else:
        log("Phase C skipped (pass --dart-dump to enable)")

    write_summary(out, args)
    log("done")


if __name__ == "__main__":
    main()
