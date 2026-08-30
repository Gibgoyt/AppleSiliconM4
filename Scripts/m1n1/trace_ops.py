#!/usr/bin/env python3
"""Parser + replay-planner for Yureka's macOS MMIO trace (pcie.log).

The trace is a 2163-line capture of macOS initializing PCIe on an M4 Mac
mini (j773g) -- the exact machine/state we chainload m1n1 on. Line 35 is
the first access to phy_ip (0x497040038), the register every RUN A..S and
1..33 AXI-stalled on, so lines 1..34 are by construction the complete
unlock preamble. Later segments carry the phy_ip PLL programming, per-port
bring-up, the LTSSM kick and the link-up poll (port+0x208 reaching
0xab000200), then ECAM enumeration.

This module is proxy-free (no m1n1 imports) so it can be unit-run offline:

    python3 Scripts/m1n1/trace_ops.py [path/to/pcie.log]

prints a parse summary plus the fully-planned preamble for eyeballing.

Format of a trace line:

    [cpu8] [0xfffffe000b7b3950] MMIO: R.4   0x497008000 (apcie[2], offset 0x8000) = 0xf7c03090
"""

import re
import sys
from collections import namedtuple

# ------------------------------------------------------------------ parsing

Op = namedtuple("Op", "line cpu kind width addr value region offset")

_LINE_RE = re.compile(
    r"\[(?P<cpu>cpu\d+)\]\s+\[0x[0-9a-f]+\]\s+MMIO:\s+"
    r"(?P<kind>[RW])\.(?P<width>[124])\s+"
    r"0x(?P<addr>[0-9a-f]+)\s+"
    r"\((?P<region>[^,]+), offset 0x(?P<offset>[0-9a-f]+)\)\s+=\s+"
    r"0x(?P<value>[0-9a-f]+)\s*$"
)


def parse_log(path):
    """Parse the whole trace into an ordered list of Op. Raises on any
    line that doesn't match (the trace format is fixed; a mismatch means
    the file isn't what we think it is)."""
    ops = []
    with open(path) as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            m = _LINE_RE.match(raw)
            if not m:
                raise ValueError(f"{path}:{lineno}: unparseable trace line: "
                                 f"{raw!r}")
            ops.append(Op(
                line=lineno,
                cpu=m.group("cpu"),
                kind=m.group("kind"),
                width=int(m.group("width")),
                addr=int(m.group("addr"), 16),
                value=int(m.group("value"), 16),
                region=m.group("region"),
                offset=int(m.group("offset"), 16),
            ))
    return ops


# ---------------------------------------------------------------- segments
#
# 1-based inclusive line ranges, hand-derived from the region-transition
# scan of pcie.log (see plan file / logs). Boundaries:
#   line 34  = W 0x497008004 (phy_shared+4) = 0x1   -- end of the unlock
#   line 35  = R 0x497040038 (phy_ip+0x38)          -- first phy_ip access
#   line 390 = R 0x494000058 (rc+0x58)              -- end of shared finish
#   line 391 = W 0x490028088                        -- port 0 bring-up start
#   line 817 = W/R 0x492028088                      -- port 2 bring-up start
#   line 1254= R 0x1cb0000000 = 0x100c106b          -- ECAM enumeration
SEGMENTS = {
    "preamble": (1, 34),
    "phyip":    (35, 390),
    "port0":    (391, 816),
    "port2":    (817, 1253),
    "enum":     (1254, None),   # None = to end of file
}


def segment_ops(ops, name):
    start, end = SEGMENTS[name]
    if end is None:
        end = ops[-1].line
    return [o for o in ops if start <= o.line <= end]


# ------------------------------------------------------------ replay planner
#
# Replay step kinds:
#   ("write", op, value)                     naked absolute write
#   ("rmw",   op, set_mask, clr_mask)        R;W pair -> read-modify-write,
#                                            preserving bits outside the
#                                            trace delta; op.value is the
#                                            trace's written value, the
#                                            preceding read is exp_pre
#   ("read",  op, expect)                    single read; compare + log
#   ("pollmask", op, mask)                   R after W to same addr showed
#                                            extra bits (ack handshake);
#                                            poll until (val & mask) == mask
#   ("pollval",  op, target, n_trace_reads)  collapsed repeated-read run;
#                                            poll until val == target
#
# Steps carry the originating Op so the replay driver can log trace line
# numbers next to every action.

Step = namedtuple("Step", "kind op a b exp_pre")
# a/b meaning per kind:  write: a=value        rmw: a=set_mask b=clr_mask
#                        read: a=expect        pollmask: a=mask
#                        pollval: a=target b=n_trace_reads

# Poll timeouts (seconds) by (region-independent) low offset within the
# port block; the +0x208 link-status poll needs real training time.
POLL_TIMEOUT_DEFAULT = 0.25
POLL_TIMEOUT_BY_PORT_OFFSET = {0x208: 3.0}


def poll_timeout_for(op):
    # Port blocks live at 0x49N028000 (N = 0/1/2); +0x208 is the link
    # status word and needs real LTSSM training time.
    if (op.addr & ~0x00f000000) == 0x490028208:
        return POLL_TIMEOUT_BY_PORT_OFFSET.get(0x208, POLL_TIMEOUT_DEFAULT)
    return POLL_TIMEOUT_DEFAULT


def plan_segment(ops):
    """Turn a raw op slice into replay steps using purely-local rules:

    1. W whose immediately-preceding trace op is an R at the same
       addr+width -> "rmw" (masks from the trace's r/w delta; preserves
       bits outside the delta on replay). This holds even when that R
       was itself emitted as a "pollmask" ack step (e.g. the CLK1REQ
       write fusing with the CLK0ACK read).
    2. R right after a W to the same addr that shows freshly-GAINED bits
       vs the written value -> "pollmask" on the gained bits
       (CLKREQ/refclk-style ack handshakes).
    3. Maximal run of consecutive R ops: group by addr; an addr whose
       reads aren't all equal, or that is read >= 3 times, collapses to
       "pollval" targeting its final value; the rest stay individual
       "read" checks. Runs like the link poll interleave two addrs
       (0x492028208 / 0x497028000) -- grouping by addr handles that.
    4. Any other W -> naked absolute "write"; any other R -> "read".
    """
    steps = []
    i = 0
    n = len(ops)
    while i < n:
        op = ops[i]
        prev = ops[i - 1] if i > 0 else None

        if op.kind == "W":
            if (prev is not None and prev.kind == "R"
                    and prev.addr == op.addr and prev.width == op.width):
                set_mask = op.value & ~prev.value
                clr_mask = prev.value & ~op.value
                steps.append(Step("rmw", op, set_mask, clr_mask, prev.value))
            else:
                steps.append(Step("write", op, op.value, None, None))
            i += 1
            continue

        # op.kind == "R"
        # Rule 2: ack poll on gained bits right after a write to this addr.
        if (prev is not None and prev.kind == "W"
                and prev.addr == op.addr and prev.width == op.width
                and (op.value & ~prev.value)):
            steps.append(Step("pollmask", op, op.value & ~prev.value,
                              None, None))
            i += 1
            continue
        # Rule 1 hand-off: leave a lone R for the following W to fuse with.
        if (i + 1 < n and ops[i + 1].kind == "W"
                and ops[i + 1].addr == op.addr
                and ops[i + 1].width == op.width):
            i += 1
            continue
        # Rule 3: maximal run of consecutive reads.
        j = i
        while j < n and ops[j].kind == "R":
            j += 1
        run = ops[i:j]
        # Don't swallow a trailing read that belongs to the NEXT rmw pair.
        if j < n and ops[j].kind == "W" and len(run) > 1 and \
                run[-1].addr == ops[j].addr and run[-1].width == ops[j].width:
            run = run[:-1]
            j -= 1
        by_addr = {}
        order = []
        for o in run:
            if o.addr not in by_addr:
                by_addr[o.addr] = []
                order.append(o.addr)
            by_addr[o.addr].append(o)
        for addr in order:
            group = by_addr[addr]
            values_differ = len({o.value for o in group}) > 1
            if len(group) >= 3 or (len(group) >= 2 and values_differ):
                final = group[-1]
                steps.append(Step("pollval", final, final.value,
                                  len(group), None))
            else:
                for o in group:
                    steps.append(Step("read", o, o.value, None, None))
        i = j
    return steps


def describe_step(s):
    op = s.op
    where = f"L{op.line:<4d} {op.kind}.{op.width} 0x{op.addr:09x} " \
            f"({op.region}+0x{op.offset:x})"
    if s.kind == "write":
        return f"{where}  WRITE  0x{s.a:x}"
    if s.kind == "rmw":
        pre = f" exp_pre=0x{s.exp_pre:x}" if s.exp_pre is not None else ""
        return (f"{where}  RMW    set=0x{s.a:x} clr=0x{s.b:x} "
                f"(trace wrote 0x{op.value:x}){pre}")
    if s.kind == "read":
        return f"{where}  READ   expect 0x{s.a:x}"
    if s.kind == "pollmask":
        return f"{where}  POLL   until (val & 0x{s.a:x}) == 0x{s.a:x}"
    if s.kind == "pollval":
        return (f"{where}  POLL   until val == 0x{s.a:x} "
                f"({s.b} reads in trace, timeout {poll_timeout_for(op)}s)")
    return f"{where}  ??? {s.kind}"


# ---------------------------------------------------------------- offline CLI

def main(argv):
    path = argv[1] if len(argv) > 1 else \
        str(__import__("pathlib").Path(__file__).resolve()
            .parents[2] / "pcie.log")
    ops = parse_log(path)
    print(f"parsed {len(ops)} ops from {path}")
    kinds = {}
    for o in ops:
        kinds[(o.kind, o.width)] = kinds.get((o.kind, o.width), 0) + 1
    for (k, w), c in sorted(kinds.items()):
        print(f"  {k}.{w}: {c}")
    print()
    for name in SEGMENTS:
        seg = segment_ops(ops, name)
        steps = plan_segment(seg)
        counts = {}
        for s in steps:
            counts[s.kind] = counts.get(s.kind, 0) + 1
        print(f"segment {name:9s}: lines {seg[0].line}..{seg[-1].line}  "
              f"{len(seg)} ops -> {len(steps)} steps  {counts}")
    print()
    print("=== planned preamble (replaces steps 6.a-6.f; lines 1-34) ===")
    for s in plan_segment(segment_ops(ops, "preamble")):
        print("  " + describe_step(s))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
