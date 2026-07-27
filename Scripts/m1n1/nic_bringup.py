#!/usr/bin/env python3
"""nic_bringup -- PLAN_3 Phase 3.5: descend behind the bridge, identify the NIC.

Runs 28-31 chased SMP into a dead end; SMP is parked (a single-core TCP server
needs exactly one core, which we have). This run pivots back to the goal.

The verified state (logs/20/nic-runtime.txt, m1n1 v1.6.0-42-gdf2ae61):

    p.pcie_init() -> 0
    port0/port2: BUSY CLEARED after 0.00s!
    00:00.0 / 00:02.0: VID:DID = 106b:100c  class=06.04.00  (Apple PCI bridges)
             bridge: primary=0 secondary=0 subordinate=0   <-- NEVER PROGRAMMED
    === enable NIC ===  No class-0x02 device found.

The two root bridges answer in ECAM but their bus numbers were never programmed,
so ecam_walk hits `secondary == 0` and never descends to the bus where the NIC
(/arm-io/apcie/pci-bridge2/lan-1gb, MAC d0:11:e5:71:81:dc) lives. The NIC has no
VID:DID in the ADT, so we cannot identify the chip -- and cannot write the Phase-5
driver -- until we read its PCI config space. That read is standard PCIe
enumeration, gated only on programming primary/secondary/subordinate at the
bridge's config +0x18.

This script:
  Phase A -- FULL PCIe bring-up first (make the link solid):
             SMC power -> CLKREQ periph mux + PERSTN deassert -> p.pcie_init()
             -> watch LINKSTS until BUSY clears. Fallback: a minimal PERST
             re-cold-reset + re-watch (PLAN_3 3.5.2). The heavier
             setup_refclk / t602x_port_init_replay live in perstn.py; if this
             happy path does not clear BUSY, fall back to
             `perstn-run.sh 20` (the proven full sequence) before RUN 32.
  Phase B -- program bridge bus numbers (the step ecam_walk never did):
             primary=0, secondary=<port index + 1>, subordinate=0xff, and
             enable the bridge command reg (MEM | BUSMASTER).
  Phase C -- re-walk. With secondary != 0 the shared ecam_walk descends
             automatically and probes the downstream bus. enable_nic() sets
             MEM+BM and sizes/reads BAR0 on the class-0x02 device.
  Phase D -- M3.5 summary: the NIC's VID:DID + class + BAR0, and a verdict.

Milestone M3.5: a real `VID:DID class=02.xx.xx` for a device behind pci-bridge2.
Commit it to m4_recon/recon-summary.md -- it decides Phase 5 (driver) or the
R2/USB-CDC-ECM fork.

Shares all low-level PCIe/ECAM primitives with perstn.py via pcie_common; the
connection + liveness helpers come from m4_common. Every config write is
guarded() + check_alive()-gated so a partial link leaves this log, not a wedge.
The dart-apcie2 / ctrl_lo overlap is NOT touched here -- that is Phase 4.

Output: /tmp/m4-recon/nic-runtime.txt
"""

import argparse
import io
import pathlib

from m4_common import (
    u, p, iface,
    log, try_, flush_partial_log, require_build,
    guarded, check_alive,
)
from pcie_regs import ApcieMap
from pcie_common import (
    PCI_COMMAND, PCI_PRIMARY_BUS, PCI_SECONDARY_BUS, PCI_SUBORDINATE,
    PCI_CMD_MEM, PCI_CMD_BM,
    PERSTN_PIN, CLKREQ_PIN,
    cfg_read16, cfg_read8, cfg_write16, cfg_write8,
    smc_power, gpio_set_periph, deassert_perstn,
    probe_device, ecam_walk, enable_nic, watch_linksts, _linksts_decode,
    poll_linksts_up, ltssm_kick,
)


def _nic_busy_clear(apcie):
    """True if the NIC's port (highest active port -- port 2 on j773g) has
    LINKSTS BUSY (bit 2) clear. Read-only; tolerant of a read fault."""
    try:
        pi = apcie.active_ports[-1]
        v = p.read32(apcie.ports[pi].port_base + 0x208)
        return not (v & (1 << 2)), pi, v
    except Exception:
        return False, None, None


def program_bridge_bus_numbers(base, bridges, buf):
    """For each root-port bridge, enable its command reg and program
    primary/secondary/subordinate so the downstream bus becomes reachable.

    secondary = <port index + 1> keeps the two ports on distinct bus numbers
    (port 0 -> bus 1, port 2 -> bus 3) so their config windows never collide.
    subordinate = 0xff opens the window wide; tighten later once the topology
    is known. Reads every value back and logs old -> new.
    """
    buf.write("\n=== program bridge bus numbers ===\n")
    for br in bridges:
        b, d, f = br["bus"], br["dev"], br["fn"]
        port = br.get("port", d)
        sec = port + 1
        tag = f"{b:02x}:{d:02x}.{f}"
        buf.write(f"--- bridge {tag} (port {port}) -> secondary bus {sec} ---\n")

        # 1. Enable MEM + BUSMASTER in the bridge command register.
        try:
            old_cmd = cfg_read16(base, b, d, f, PCI_COMMAND)
            new_cmd = old_cmd | PCI_CMD_MEM | PCI_CMD_BM
            cfg_write16(base, b, d, f, PCI_COMMAND, new_cmd)
            rb = cfg_read16(base, b, d, f, PCI_COMMAND)
            buf.write(f"  CMD: 0x{old_cmd:04x} -> 0x{new_cmd:04x} "
                      f"(readback 0x{rb:04x})\n")
        except Exception as e:
            buf.write(f"  CMD write failed: {e.__class__.__name__}: {e}\n")
            continue

        # 2. Program the three bus-number bytes at +0x18/0x19/0x1a.
        try:
            old_p = cfg_read8(base, b, d, f, PCI_PRIMARY_BUS)
            old_s = cfg_read8(base, b, d, f, PCI_SECONDARY_BUS)
            old_u = cfg_read8(base, b, d, f, PCI_SUBORDINATE)
            cfg_write8(base, b, d, f, PCI_PRIMARY_BUS, 0)
            cfg_write8(base, b, d, f, PCI_SECONDARY_BUS, sec)
            cfg_write8(base, b, d, f, PCI_SUBORDINATE, 0xff)
            new_p = cfg_read8(base, b, d, f, PCI_PRIMARY_BUS)
            new_s = cfg_read8(base, b, d, f, PCI_SECONDARY_BUS)
            new_u = cfg_read8(base, b, d, f, PCI_SUBORDINATE)
            buf.write(f"  primary:     0x{old_p:02x} -> 0x{new_p:02x} "
                      f"(want 0x00)\n")
            buf.write(f"  secondary:   0x{old_s:02x} -> 0x{new_s:02x} "
                      f"(want 0x{sec:02x})\n")
            buf.write(f"  subordinate: 0x{old_u:02x} -> 0x{new_u:02x} "
                      f"(want 0xff)\n")
            # Record the bus we opened so ecam_walk descends to it.
            br["secondary"] = new_s
            br["subordinate"] = new_u
        except Exception as e:
            buf.write(f"  bus-number write failed: {e.__class__.__name__}: {e}\n")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.strip().splitlines()[0])
    ap.add_argument("--out", default="/tmp/m4-recon",
                    help="output directory (default: /tmp/m4-recon)")
    ap.add_argument("--require-build", default="",
                    help="abort unless the enrolled m1n1 USB product string "
                         "contains this substring (e.g. v1.6.0-42-g)")
    ap.add_argument("--dump-timeout", type=float, default=0.3,
                    help="UART timeout (s) during guarded config spans "
                         "(default: 0.3)")
    ap.add_argument("--no-bringup", action="store_true",
                    help="skip Phase A (SMC/GPIO/pcie_init/watch); assume the "
                         "link is already up from a prior run this boot")
    ap.add_argument("--link-watch-secs", type=float, default=5.0,
                    help="seconds to watch LINKSTS for BUSY-clear (default: 5)")
    ap.add_argument("--link-up-secs", type=float, default=2.0,
                    help="seconds to poll LINKSTS for UP (bit0) on the NIC port "
                         "before/after the LTSSM kick (default: 2)")
    ap.add_argument("--no-ltssm-kick", action="store_true",
                    help="skip Phase A.2 (LINKSTS_UP gate + host-side LTSSM "
                         "kick); go straight to bus programming + descend")
    args = ap.parse_args()

    require_build(args.require_build)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    runtime_path = out / "nic-runtime.txt"

    buf = io.StringIO()
    timeout = args.dump_timeout

    def flush(tag):
        flush_partial_log(runtime_path, buf, tag)

    def liveness_gate(next_label):
        if check_alive():
            return True
        log(f"m1n1 is wedged; skipping {next_label}")
        buf.write(f"SKIPPED {next_label}: m1n1 not responding.\n")
        return False

    buf.write("=== nic_bringup: PLAN_3 Phase 3.5 (identify the NIC) ===\n\n")

    log("building ApcieMap from ADT...")
    apcie = ApcieMap.from_adt(u)
    buf.write(f"ecam_base   = 0x{apcie.ecam_base:x}\n")
    buf.write(f"active ports = {apcie.active_ports}\n\n")
    flush("apcie-map")

    # ---------------------------------------------------------------- Phase A
    if not args.no_bringup:
        log("=== Phase A: full PCIe bring-up ===")
        buf.write("=== Phase A: PCIe bring-up ===\n")

        # Resolve NIC PERSTN / CLKREQ pins from the ADT (fallback to constants).
        nic_port = apcie.nic_port()
        perstn_pin = PERSTN_PIN
        clkreq_pin = CLKREQ_PIN
        if nic_port is not None:
            if nic_port.perst_pin is not None:
                perstn_pin = nic_port.perst_pin
            if nic_port.clkreq_pin is not None:
                clkreq_pin = nic_port.clkreq_pin
        buf.write(f"NIC GPIO pins: PERSTN=gpio0[{perstn_pin}] "
                  f"CLKREQ=gpio0[{clkreq_pin}]\n\n")

        log("SMC power on apcie fabric...")
        try_(lambda: smc_power(buf), "SMC power")
        flush("smc-power")

        # CLKREQ# -> ADT-declared peripheral function 2 (controller drives it).
        log(f"CLKREQ -> periph func 2 on gpio0 pin {clkreq_pin}...")
        try_(lambda: gpio_set_periph(clkreq_pin, 2, buf), "gpio_set_periph(clkreq)")
        flush("clkreq-periph")

        log(f"PERSTN deassert on gpio0 pin {perstn_pin}...")
        try_(lambda: deassert_perstn(buf, pin=perstn_pin), "deassert_perstn")
        flush("perstn")

        # pcie_init can legitimately take many seconds; a short UART timeout
        # would desync the proxy and look like a wedge. Give it 60 s (mirrors
        # perstn.py's window around the same call).
        log("p.pcie_init()...")
        _old_to = iface.dev.timeout
        iface.dev.timeout = 60.0
        try:
            rc = try_(lambda: p.pcie_init(), "pcie_init")
            buf.write(f"\np.pcie_init() -> {rc!r}\n\n")
        finally:
            iface.dev.timeout = _old_to
        flush("pcie-init")

        if liveness_gate("post-init LINKSTS watch"):
            with guarded(buf, "watch_linksts", short_timeout=timeout):
                try_(lambda: watch_linksts(apcie, buf,
                                           secs=args.link_watch_secs),
                     "watch_linksts")
            flush("linksts-watch")

        # Fallback (PLAN_3 3.5.2): if the NIC port is still BUSY, re-cold-reset
        # PERSTN once and re-watch. Deeper fabric coaxing (setup_refclk /
        # t602x replay) lives in perstn.py -- run `perstn-run.sh 20` for that.
        clear, pi, v = _nic_busy_clear(apcie)
        if not clear and liveness_gate("PERST re-sequence fallback"):
            log("NIC port still BUSY -- PERST re-cold-reset fallback...")
            buf.write("\n=== PERST re-cold-reset fallback (3.5.2) ===\n")
            if v is not None:
                buf.write(f"  port{pi} LINKSTS=0x{v:08x} "
                          f"[{_linksts_decode(v)}] still BUSY\n")
            try_(lambda: deassert_perstn(buf, pin=perstn_pin), "deassert_perstn(retry)")
            with guarded(buf, "watch_linksts(retry)", short_timeout=timeout):
                try_(lambda: watch_linksts(apcie, buf, secs=args.link_watch_secs,
                                           label="post-perst-retry"),
                     "watch_linksts(retry)")
            flush("perst-retry")

        # Phase A.2: LINK-UP gate. BUSY clearing means the port controller ran,
        # but the DOWNSTREAM link may still be in Detect (endpoint config not
        # ready -> reads fault). m1n1's C waits only for PORT_STATUS_RUN /
        # !BUSY, never LINKSTS_UP (bit0), and SKIPS the T602X LTSSM kick for
        # T8132. Poll UP here; if not up, replay the kick host-side and re-poll.
        nic_pi = apcie.active_ports[-1]  # port 2 on j773g
        if not args.no_ltssm_kick and liveness_gate("LINKSTS_UP gate"):
            buf.write("\n=== Phase A.2: downstream link-up gate ===\n")
            with guarded(buf, "poll_linksts_up", short_timeout=timeout):
                up, _ = try_(lambda: poll_linksts_up(apcie, nic_pi, buf,
                                                     secs=args.link_up_secs),
                             "poll_linksts_up") or (False, None)
            flush("linkup-poll")
            if not up and liveness_gate("LTSSM kick"):
                log(f"port{nic_pi} not UP -- replaying T602X LTSSM kick...")
                buf.write("\n--- LTSSM kick (T602X sequence, skipped for T8132 "
                          "in C) ---\n")
                with guarded(buf, "ltssm_kick", short_timeout=timeout):
                    try_(lambda: ltssm_kick(apcie, nic_pi, buf), "ltssm_kick")
                with guarded(buf, "poll_linksts_up(post-kick)",
                             short_timeout=timeout):
                    try_(lambda: poll_linksts_up(apcie, nic_pi, buf,
                                                 secs=args.link_up_secs),
                         "poll_linksts_up(post-kick)")
                flush("ltssm-kick")
    else:
        log("Phase A skipped (--no-bringup): assuming link already up.")
        buf.write("=== Phase A skipped (--no-bringup) ===\n\n")

    base = apcie.ecam_base

    # ---------------------------------------------------------------- Phase B
    # Initial walk of root bus 0: find the bridges (00:00.0 / 00:02.0).
    bridges = []
    if liveness_gate("initial root-bus walk"):
        log("initial root-bus walk (find bridges)...")
        buf.write("\n=== initial root-bus walk ===\n")
        with guarded(buf, "root-walk", short_timeout=timeout):
            for d in apcie.active_ports:
                info = try_(lambda d=d: probe_device(base, 0, d, 0, buf),
                            f"cfg 00:{d:02x}.0")
                if info is not None:
                    info["port"] = d
                    # Only descend behind actual PCI-PCI bridges (header 0x01).
                    if (info.get("header_type", 0) & 0x7f) == 0x01:
                        bridges.append(info)
        flush("root-walk")

    if bridges and liveness_gate("program bridge bus numbers"):
        log("=== Phase B: program bridge bus numbers ===")
        with guarded(buf, "program_bridge_bus_numbers", short_timeout=timeout):
            try_(lambda: program_bridge_bus_numbers(base, bridges, buf),
                 "program_bridge_bus_numbers")
        flush("bus-numbers")
    elif not bridges:
        buf.write("\nNo PCI-PCI bridges found on root bus 0 -- cannot descend. "
                  "Is the link up? Try `perstn-run.sh 20` for the full "
                  "bring-up sequence.\n")

    # ---------------------------------------------------------------- Phase C
    devices = []
    if liveness_gate("ECAM re-walk"):
        log(f"=== Phase C: ECAM re-walk @ 0x{base:x} ===")
        with guarded(buf, "ecam_walk", short_timeout=timeout):
            devices = try_(lambda: ecam_walk(base, buf,
                                             active_ports=apcie.active_ports),
                           "ecam_walk") or []
        flush("ecam-walk")

    # Pick the NIC: class-0x02 device on any downstream bus.
    nic = None
    for dv in devices:
        if dv.get("cls_base") == 0x02:
            nic = dv
            break

    if nic is not None and liveness_gate("enable_nic"):
        with guarded(buf, "enable_nic", short_timeout=timeout):
            try_(lambda: enable_nic(base, nic, buf), "enable_nic")
        flush("enable-nic")

    # ---------------------------------------------------------------- Phase D
    buf.write("\n=== M3.5 summary ===\n")
    if nic is not None:
        vid, did = nic["vid"], nic["did"]
        cls = (f"{nic.get('cls_base', 0xff):02x}."
               f"{nic.get('cls_dev', 0xff):02x}."
               f"{nic.get('cls_prog', 0xff):02x}")
        bar0 = nic.get("bar0_masked", nic.get("bar0", 0))
        tag = f"{nic['bus']:02x}:{nic['dev']:02x}.{nic['fn']}"
        verdict = (
            f"M3.5 PASS: NIC at {tag} VID:DID={vid:04x}:{did:04x} class={cls} "
            f"BAR0=0x{bar0:08x}. "
            f"Commit VID:DID to m4_recon/recon-summary.md (closes Q2/Q3). "
            f"VID {vid:04x} decides Phase 5: discrete driver (5A) vs Apple "
            f"silicon / USB-CDC-ECM fork (5B, if VID==106b)."
        )
    else:
        # Diagnose why: bridges found but no descendant, or no bridges at all.
        if not bridges:
            verdict = ("M3.5 NOT MET: no bridges on root bus 0 -- link almost "
                       "certainly not up. Run `perstn-run.sh 20` (proven full "
                       "bring-up), confirm pcie_init()->0 + BUSY CLEARED, then "
                       "re-run RUN 32.")
        else:
            secs = ", ".join(f"{b['bus']:02x}:{b['dev']:02x}.{b['fn']}="
                             f"sec0x{b.get('secondary', 0):02x}"
                             for b in bridges)
            verdict = ("M3.5 NOT MET: bridges up + bus numbers programmed "
                       f"({secs}) but no class-0x02 device answered on the "
                       "downstream bus. Fallbacks (PLAN_3 3.5.2): (1) link not "
                       "fully trained -- poll LINKSTS bit0 / re-perst; (2) "
                       "re-test phy_ip from the post-BIT(4) state; (3) recheck "
                       "PERST#/refclk timing via `perstn-run.sh 20/21`.")
    buf.write(verdict + "\n")
    log(verdict)

    runtime_path.write_text(buf.getvalue())
    log(f"done. wrote {runtime_path}. Copy into m4_recon/ if it looks sane.")


if __name__ == "__main__":
    main()
