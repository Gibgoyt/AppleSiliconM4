"""ADT-driven register map for /arm-io/apcie on t8132 (Apple M4).

Reads every reg entry from the ADT rather than hardcoding literals, so if
Apple ever revises the layout in a firmware update, the field bases move
too. Source: m4_recon/adt.txt lines 1391-1443 (apcie 'reg' property) and
lines 2488-2589 (pci-bridge{0,2} nodes).

## t8132 apcie 'reg' layout (25 entries)

Shared (indices 0..6):
    [0] ECAM         0x1cb0000000  sz 0x10000000
    [1] RC           0x494000000   sz 0x4000       -- 'root complex' block
    [2] PHY packed   0x497000000   sz 0x40000      -- contains phy_common at
                                                     +0x4000 and per-port
                                                     port_phy_base at
                                                     +0x8000 + N * 0x4000
    [3] PHY IP       0x497040000   sz 0x20000      -- shared PHY IP block
                                                     (apcie-phy-ip-* tunables
                                                     land here)
    [4] AXI          0x496000000   sz 0x1000000    -- AXI2AF fabric registers
    [5] AXI subrange 0x495046200   sz 0x4000       -- purpose unknown, sits
                                                     inside AXI window
    [6] AXI subrange 0x495044000   sz 0x4000       -- purpose unknown, sits
                                                     inside AXI window

Per-port (6 entries each, indices 7..24; N = 0/1/2):
    [0] port_base       0x49N028000  sz 0x8000     -- port controller regs
                                                     (APPCLK/STATUS/LINKSTS/...)
    [1] port_ltssm_base 0x49N03c000  sz 0x4000     -- LTSSM debug block
    [2] port_phy_base   0x497020000+ sz 0x4000     -- per-port PHY, packed
                                                     into shared PHY block
    [3] phy_extra       0x497048000+ sz 0x8000     -- per-port PHY IP slice;
                                                     overlaps shared PHY IP
                                                     (reg[3]) at +0x8000
                                                     stride. NEW in t8132;
                                                     m1n1's t8140 path does
                                                     not touch this
                                                     explicitly, but the
                                                     apcie-phy-ip-*-tunables
                                                     writes DO land here
                                                     because their offsets
                                                     (0xA000, 0x12000,
                                                     0x1A000) hit inside
                                                     these slices when
                                                     applied to phy_ip_base
                                                     (reg[3]).
    [4] port_intr2axi   0x49N024000  sz 0x4000     -- interrupt to AXI bridge
    [5] ctrl_lo         0x49N000000  sz 0xc000     -- **OVERLAPS the DART
                                                     MMIO for this port**.
                                                     dart-apcie{0,2} sits at
                                                     0x490000000 / 0x492000000
                                                     with size 0x20000, so
                                                     the first 48 KB of each
                                                     DART is aliased into
                                                     apcie's reg map. On
                                                     j773g we only touch
                                                     these read-only; writes
                                                     go through DART.from_adt
                                                     in Phase 4.

## GPIO wiring per port (from pci-bridge{N})

    port 0 (WiFi+BT): perst=gpio0[0xa3=163], clkreq=gpio0[0xa0=160], mode 2
    port 1: NO pci-bridge1 in ADT on j773g
    port 2 (1 GbE NIC): perst=gpio0[0xa5=165], clkreq=gpio0[0xa2=162], mode 2

    ADT function-clkreq mode = 2 means "hand pin to PCIe controller as
    peripheral function". Driving it as manual GPIO output overrides that
    muxing; use with care.
"""

import struct

# ---------------------------------------------- phy_ip_base slice geometry
#
# On T8140/T8132 the shared `phy_ip_base` window (reg[3], size 0x20000) is
# partitioned as:
#     [0x00000, 0x08000)  -- shared PLL area
#                            (used by apcie-phy-ip-pll-tunables)
#     [0x08000, 0x10000)  -- port 0 PHY IP slice
#                            (aliased to phy_extra_base[0])
#     [0x10000, 0x18000)  -- port 1 PHY IP slice
#                            (aliased to phy_extra_base[1])
#     [0x18000, 0x20000)  -- port 2 PHY IP slice
#                            (aliased to phy_extra_base[2])
#
# The Apple-supplied apcie-phy-ip-{pll,auspma}-tunables blob is a single
# un-indexed list whose entries have absolute offsets from phy_ip_base --
# writes at offsets in a port's slice are "for" that port. On j773g port 1
# has no pci-bridge1 node and its PHY IP slice is decoded but unpowered,
# so writes in [0x10000, 0x18000) AXI-stall m1n1. See PLAN.md § 3.3.
PHY_IP_SLICE_BASE   = 0x8000
PHY_IP_SLICE_STRIDE = 0x8000
PHY_IP_WINDOW_SIZE  = 0x20000

# ------------------------------------------------------------------ tunables

# Apple ADT tunable entry format is 24 bytes (LE):
#   u32 offset, u32 size, u64 mask, u64 value
_TUNABLE_STRUCT = struct.Struct("<IIQQ")


def parse_tunables_hex(hexstr):
    """Parse a raw hex-string tunables blob (as seen on pci-bridge*.
    apcie-config-tunables, pcie-rc-tunables etc.) into a list of
    (offset, size, mask, value) tuples."""
    if hexstr is None:
        return []
    if isinstance(hexstr, (bytes, bytearray)):
        data = bytes(hexstr)
    else:
        data = bytes.fromhex(hexstr)
    if len(data) % _TUNABLE_STRUCT.size:
        raise ValueError(f"tunables blob length {len(data)} not a multiple "
                         f"of {_TUNABLE_STRUCT.size}")
    return [_TUNABLE_STRUCT.unpack_from(data, i)
            for i in range(0, len(data), _TUNABLE_STRUCT.size)]


def parse_tunables_container(prop):
    """Parse the already-decoded Container form (list of dicts / attrs) that
    proxyclient produces for parsed ADT tunables properties like
    apcie-phy-tunables."""
    if prop is None:
        return []
    out = []
    for e in prop:
        offset = int(e["offset"] if isinstance(e, dict) else e.offset)
        size = int(e["size"] if isinstance(e, dict) else e.size)
        mask = int(e["mask"] if isinstance(e, dict) else e.mask)
        value = int(e["value"] if isinstance(e, dict) else e.value)
        out.append((offset, size, mask, value))
    return out


# ---------------------------------------------------------------- data classes


class PortMap:
    """Per-port MMIO handles + GPIO wiring for one apcie port."""

    def __init__(self, index):
        self.index = index
        # Set by ApcieMap.from_adt() below.
        self.port_base = 0
        self.port_size = 0
        self.ltssm_base = 0
        self.ltssm_size = 0
        self.phy_base = 0        # per-port PHY inside packed PHY block
        self.phy_size = 0
        self.phy_extra_base = 0  # per-port PHY IP slice
        self.phy_extra_size = 0
        self.intr2axi_base = 0
        self.intr2axi_size = 0
        self.ctrl_lo_base = 0    # overlaps DART MMIO
        self.ctrl_lo_size = 0
        # pci-bridge{N} node (or None if absent, like port 1 on j773g).
        self.exists = False
        self.perst_pin = None
        self.perst_mode = None   # ADT function-perst mode (arg[1]); 0 = GPIO
        self.clkreq_pin = None
        self.clkreq_mode = None  # ADT function-clkreq mode; 2 = peripheral
        self.max_link_speed = None
        self.bridge_path = None
        # Bridge-scoped tunables blobs.
        self.apcie_config_tunables = []
        self.pcie_rc_tunables = []
        self.pcie_rc_gen3_shadow_tunables = []
        self.pcie_rc_gen4_shadow_tunables = []

    def describe(self, out):
        tag = f"port{self.index}"
        out.write(f"--- {tag} ---\n")
        out.write(f"  exists in ADT: {self.exists}"
                  f"{' (pci-bridge missing)' if not self.exists else ''}\n")
        out.write(f"  port_base       = 0x{self.port_base:x} "
                  f"sz 0x{self.port_size:x}\n")
        out.write(f"  ltssm_base      = 0x{self.ltssm_base:x} "
                  f"sz 0x{self.ltssm_size:x}\n")
        out.write(f"  phy_base        = 0x{self.phy_base:x} "
                  f"sz 0x{self.phy_size:x}\n")
        out.write(f"  phy_extra_base  = 0x{self.phy_extra_base:x} "
                  f"sz 0x{self.phy_extra_size:x}   "
                  f"(inside shared PHY IP)\n")
        out.write(f"  intr2axi_base   = 0x{self.intr2axi_base:x} "
                  f"sz 0x{self.intr2axi_size:x}\n")
        out.write(f"  ctrl_lo_base    = 0x{self.ctrl_lo_base:x} "
                  f"sz 0x{self.ctrl_lo_size:x}   "
                  f"(overlaps dart-apcie{self.index})\n")
        if self.exists:
            out.write(f"  bridge          = {self.bridge_path}\n")
            out.write(f"  PERSTN pin      = gpio0[{self.perst_pin}] "
                      f"(mode {self.perst_mode})\n")
            out.write(f"  CLKREQ pin      = gpio0[{self.clkreq_pin}] "
                      f"(mode {self.clkreq_mode})\n")
            out.write(f"  max_link_speed  = {self.max_link_speed}\n")
            out.write(f"  #apcie_config_tunables       = "
                      f"{len(self.apcie_config_tunables)}\n")
            out.write(f"  #pcie_rc_tunables            = "
                      f"{len(self.pcie_rc_tunables)}\n")
            out.write(f"  #pcie_rc_gen3_shadow_tunables= "
                      f"{len(self.pcie_rc_gen3_shadow_tunables)}\n")
            out.write(f"  #pcie_rc_gen4_shadow_tunables= "
                      f"{len(self.pcie_rc_gen4_shadow_tunables)}\n")


class DartInfo:
    """Handle for one dart-apcie{N} node. Overlaps ctrl_lo of the same port."""

    def __init__(self, node_name, base, size, sid_count, page_size):
        self.node_name = node_name
        self.base = base
        self.size = size
        self.sid_count = sid_count
        self.page_size = page_size


class ApcieMap:
    """Complete t8132 /arm-io/apcie MMIO map + GPIO wiring, sourced from ADT.

    Build one with ApcieMap.from_adt(u) from a proxyclient session where u.adt
    is populated.
    """

    N_PORTS = 3
    SHARED_REG_COUNT = 7        # reg[0..6]
    PER_PORT_REG_COUNT = 6      # reg[7 + 6*port + 0..5]

    def __init__(self):
        # Shared regs.
        self.ecam_base = self.ecam_size = 0
        self.rc_base = self.rc_size = 0
        self.phy_packed_base = self.phy_packed_size = 0
        self.phy_ip_base = self.phy_ip_size = 0
        self.axi_base = self.axi_size = 0
        self.axi_sub5_base = self.axi_sub5_size = 0   # reg[5]  0x495046200
        self.axi_sub6_base = self.axi_sub6_size = 0   # reg[6]  0x495044000
        # Derived shared bases (match m1n1 t8140 path after the +0x8000 /
        # +0x4000 correction at pcie.c:394).
        self.phy_common_base = 0     # phy_packed + 0x4000
        # Per-port maps.
        self.ports = [PortMap(i) for i in range(self.N_PORTS)]
        # DART siblings (may be absent for a given port).
        self.darts = {}   # port_index -> DartInfo
        # ADT compatible string, so callers can sanity-check.
        self.compatible = None
        # 'power-gates' from ADT (PMGR device indices to enable).
        self.power_gates = ()

    # ------------------------------------------------------- construction

    @classmethod
    def from_adt(cls, u):
        self = cls()
        apcie = u.adt["arm-io/apcie"]

        # -- compatible + power-gates
        try:
            compat = apcie.compatible
            if isinstance(compat, (list, tuple)):
                compat = compat[0]
            self.compatible = str(compat)
        except AttributeError:
            self.compatible = "<unknown>"
        try:
            pg = apcie.power_gates
            self.power_gates = tuple(pg) if pg is not None else ()
        except AttributeError:
            self.power_gates = ()

        # -- shared regs
        (self.ecam_base, self.ecam_size)          = apcie.get_reg(0)
        (self.rc_base, self.rc_size)              = apcie.get_reg(1)
        (self.phy_packed_base, self.phy_packed_size) = apcie.get_reg(2)
        (self.phy_ip_base, self.phy_ip_size)      = apcie.get_reg(3)
        (self.axi_base, self.axi_size)            = apcie.get_reg(4)
        (self.axi_sub5_base, self.axi_sub5_size)  = apcie.get_reg(5)
        (self.axi_sub6_base, self.axi_sub6_size)  = apcie.get_reg(6)

        # Match m1n1 t8140 correction: phy_common lives at phy_packed + 0x4000.
        self.phy_common_base = self.phy_packed_base + 0x4000

        # -- per-port regs
        for i, port in enumerate(self.ports):
            base_idx = cls.SHARED_REG_COUNT + i * cls.PER_PORT_REG_COUNT
            (port.port_base, port.port_size)          = apcie.get_reg(base_idx + 0)
            (port.ltssm_base, port.ltssm_size)        = apcie.get_reg(base_idx + 1)
            (port.phy_base, port.phy_size)            = apcie.get_reg(base_idx + 2)
            (port.phy_extra_base, port.phy_extra_size) = apcie.get_reg(base_idx + 3)
            (port.intr2axi_base, port.intr2axi_size)  = apcie.get_reg(base_idx + 4)
            (port.ctrl_lo_base, port.ctrl_lo_size)    = apcie.get_reg(base_idx + 5)

        # -- pci-bridge{N} nodes (may be absent, e.g. port 1 on j773g)
        for i, port in enumerate(self.ports):
            bridge_path = f"arm-io/apcie/pci-bridge{i}"
            try:
                bridge = u.adt[bridge_path]
            except (KeyError, IndexError, AttributeError):
                bridge = None
            if bridge is None:
                continue
            port.exists = True
            port.bridge_path = "/" + bridge_path
            port.perst_pin, port.perst_mode = _parse_gpio_prop(
                bridge, "function-perst")
            port.clkreq_pin, port.clkreq_mode = _parse_gpio_prop(
                bridge, "function-clkreq")
            port.max_link_speed = _try_prop(bridge, "maximum-link-speed")
            port.apcie_config_tunables = _grab_tunables(
                bridge, "apcie-config-tunables")
            port.pcie_rc_tunables = _grab_tunables(bridge, "pcie-rc-tunables")
            port.pcie_rc_gen3_shadow_tunables = _grab_tunables(
                bridge, "pcie-rc-gen3-shadow-tunables")
            port.pcie_rc_gen4_shadow_tunables = _grab_tunables(
                bridge, "pcie-rc-gen4-shadow-tunables")

        # -- DART overlays (dart-apcie{N})
        for i in range(self.N_PORTS):
            for dart_name in (f"arm-io/dart-apcie{i}",):
                try:
                    d = u.adt[dart_name]
                except (KeyError, IndexError, AttributeError):
                    continue
                addr, size = d.get_reg(0)
                self.darts[i] = DartInfo(
                    node_name="/" + dart_name,
                    base=addr,
                    size=size,
                    sid_count=_try_prop(d, "sid-count"),
                    page_size=_try_prop(d, "page-size"),
                )

        return self

    # ------------------------------------------------------------ helpers

    @property
    def active_ports(self):
        """List of port indices whose pci-bridge{N} exists in the ADT. On
        j773g this is [0, 2] (WiFi/BT + 1 GbE NIC)."""
        return [p.index for p in self.ports if p.exists]

    @property
    def has_nic(self):
        return any(p.exists and p.max_link_speed is not None
                   for p in self.ports)

    def nic_port(self):
        """Return the PortMap for the 1 GbE NIC (port 2 on j773g), or None.
        Heuristic: pick the highest-indexed active port that has the NIC on
        it. In practice on j773g the caller wants port 2."""
        for p in reversed(self.ports):
            if p.exists and p.index == 2:
                return p
        for p in reversed(self.ports):
            if p.exists:
                return p
        return None

    def apcie_tunables(self, u, prop):
        """Fetch an apcie-node-level tunables property (parsed Container form)."""
        apcie = u.adt["arm-io/apcie"]
        val = _try_prop(apcie, prop)
        return parse_tunables_container(val)

    def classify_phy_ip_offset(self, offset):
        """Classify a tunable offset (from apcie-phy-ip-* lists) against
        the phy_ip_base slice geometry. See PHY_IP_* constants at the top
        of this module for the layout on T8140/T8132.

        Returns a dict:
            target_addr        -- phy_ip_base + offset (int)
            kind               -- "shared" | "port_slice" | "out_of_window"
            port_index         -- 0/1/2 or None (only for "port_slice")
            port_active        -- True/False or None (only for "port_slice";
                                  True iff pci-bridge{N} exists in ADT)
            slice_base_offset  -- offset of the start of the slice, or None
            slice_size         -- slice length in bytes, or None
        """
        target_addr = self.phy_ip_base + offset
        if offset < 0 or offset >= PHY_IP_WINDOW_SIZE:
            return dict(target_addr=target_addr, kind="out_of_window",
                        port_index=None, port_active=None,
                        slice_base_offset=None, slice_size=None)
        if offset < PHY_IP_SLICE_BASE:
            return dict(target_addr=target_addr, kind="shared",
                        port_index=None, port_active=None,
                        slice_base_offset=0,
                        slice_size=PHY_IP_SLICE_BASE)
        rel = offset - PHY_IP_SLICE_BASE
        idx = rel // PHY_IP_SLICE_STRIDE
        slice_base = PHY_IP_SLICE_BASE + idx * PHY_IP_SLICE_STRIDE
        active = (0 <= idx < len(self.ports)) and self.ports[idx].exists
        return dict(target_addr=target_addr, kind="port_slice",
                    port_index=idx, port_active=active,
                    slice_base_offset=slice_base,
                    slice_size=PHY_IP_SLICE_STRIDE)

    # ------------------------------------------------------------ dump

    def describe(self, out):
        out.write("=== apcie ADT map ===\n")
        out.write(f"compatible   = {self.compatible}\n")
        out.write(f"power_gates  = {list(self.power_gates)}\n")
        out.write(f"active ports = {self.active_ports}\n\n")

        out.write("-- shared --\n")
        out.write(f"  ecam_base       = 0x{self.ecam_base:x} "
                  f"sz 0x{self.ecam_size:x}\n")
        out.write(f"  rc_base         = 0x{self.rc_base:x} "
                  f"sz 0x{self.rc_size:x}\n")
        out.write(f"  phy_packed_base = 0x{self.phy_packed_base:x} "
                  f"sz 0x{self.phy_packed_size:x}   "
                  f"(phy_common = +0x4000)\n")
        out.write(f"  phy_common_base = 0x{self.phy_common_base:x}\n")
        out.write(f"  phy_ip_base     = 0x{self.phy_ip_base:x} "
                  f"sz 0x{self.phy_ip_size:x}\n")
        out.write(f"  axi_base        = 0x{self.axi_base:x} "
                  f"sz 0x{self.axi_size:x}\n")
        out.write(f"  axi_sub5        = 0x{self.axi_sub5_base:x} "
                  f"sz 0x{self.axi_sub5_size:x}   "
                  f"(inside AXI window; purpose unknown)\n")
        out.write(f"  axi_sub6        = 0x{self.axi_sub6_base:x} "
                  f"sz 0x{self.axi_sub6_size:x}   "
                  f"(inside AXI window; purpose unknown)\n\n")

        for port in self.ports:
            port.describe(out)
            out.write("\n")

        if self.darts:
            out.write("-- darts --\n")
            for i, d in sorted(self.darts.items()):
                out.write(f"  dart[port {i}]: {d.node_name}  "
                          f"base=0x{d.base:x} sz=0x{d.size:x}  "
                          f"page_size={d.page_size}  "
                          f"sid_count={d.sid_count}\n")
            out.write("\n")


# ---------------------------------------------------------------- ADT helpers


def _try_prop(node, name):
    """Fetch an ADT property by name (with underscore fallback for hyphens),
    returning None if missing. Handles both attribute and dict-style access."""
    py_name = name.replace("-", "_")
    for candidate in (name, py_name):
        try:
            return getattr(node, candidate)
        except AttributeError:
            pass
        try:
            return node[candidate]
        except (KeyError, TypeError):
            pass
    return None


def _grab_tunables(node, prop_name):
    val = _try_prop(node, prop_name)
    if val is None:
        return []
    if isinstance(val, (bytes, bytearray)):
        return parse_tunables_hex(val)
    if isinstance(val, str):
        try:
            return parse_tunables_hex(val)
        except ValueError:
            return []
    return parse_tunables_container(val)


def _parse_gpio_prop(node, prop_name):
    """Extract (pin, mode) from a `function-*` GPIO property like
    `120:GPIO(0xa5, 0x0)`. Returns (None, None) if the property is missing
    or unparseable."""
    val = _try_prop(node, prop_name)
    if val is None:
        return None, None

    if isinstance(val, str):
        # Text form from adt.txt: "120:GPIO(0xa5, 0x0)"
        try:
            args = val.split("GPIO(")[1].split(")", 1)[0]
            pin_s, mode_s = [s.strip() for s in args.split(",")]
            return int(pin_s, 0), int(mode_s, 0)
        except (IndexError, ValueError):
            return None, None

    # Object form from proxyclient's ADT parser.
    args = _try_prop(val, "args")
    if args is None and hasattr(val, "__iter__"):
        try:
            args = list(val)
        except TypeError:
            args = None
    if args and len(args) >= 2:
        try:
            return int(args[0]), int(args[1])
        except (TypeError, ValueError):
            return None, None
    return None, None
