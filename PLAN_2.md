# PLAN_2 — TCP "Hello World" Ping-Pong Kernel on Apple M4, Hosted by m1n1

> **Concrete goal:** From the Comet Lake dev host (`192.168.0.251`), run
> `nc 192.168.0.246 3333` and receive `Hello World\n` back from a bare-metal
> AArch64 kernel running on an M4 Mac mini (Mac16,10 / T8132), served over
> the M4's built-in wired Ethernet port.
>
> **Not in scope for this milestone:** MMU, SMP, IRQs, filesystem, USB
> as a network transport, macOS coexistence at runtime, security. Deferred.
>
> **Strategic pivot vs. `PLAN.md`:** the older plan assumed a full chainload
> replacement of m1n1 with our kernel, then bringing PCIe + DART + NIC up
> from scratch. This plan keeps m1n1 alive as a debug host and reuses
> `p.pcie_init()` / `p.dart_init()` / `p.dart_map()` so we only write the
> NIC-specific bits + TCP glue. That trades a "pure" bare-metal boot for a
> ~10× shorter iteration loop and a real debug console.

---

## Table of contents

- [§0 Rules of engagement](#0-rules-of-engagement)
- [§1 Ground truth (M4 mini hardware and boot state)](#1-ground-truth)
- [§2 Strategy — kernel as m1n1 `p.call()` payload](#2-strategy)
- [§3 Reference map (every AsahiLinux file:line we depend on)](#3-reference-map)
- [§4 Environment prep on the Comet Lake dev host](#4-environment-prep)
- [§5 Phase 1 — Reconnaissance (dump everything)](#5-phase-1--reconnaissance)
- [§6 Phase 2 — Rebuild the kernel as a callable payload](#6-phase-2--kernel-as-payload)
- [§7 Phase 3 — PCIe reach (surface the NIC)](#7-phase-3--pcie-reach)
- [§8 Phase 4 — DART for the NIC (DMA)](#8-phase-4--dart-for-nic)
- [§9 Phase 5 — NIC driver (bare-metal C)](#9-phase-5--nic-driver)
- [§10 Phase 6 — ARP (be discoverable)](#10-phase-6--arp)
- [§11 Phase 7 — lwIP integration](#11-phase-7--lwip)
- [§12 Phase 8 — TCP hello world server on port 3333](#12-phase-8--tcp)
- [§13 Debugging strategy](#13-debugging-strategy)
- [§14 Risks and pivot points](#14-risks)
- [§15 First-session concrete commands](#15-first-session-commands)

---

<a id="0-rules-of-engagement"></a>
## §0 Rules of engagement

1. **Never edit `PLAN.md`.** It stays as historical reference. This document
   (`PLAN_2.md`) is the live plan.
2. **All new AsahiLinux code we ship must be original.** m1n1 is MIT/BSD —
   we may lift small helpers with attribution. Asahi Linux kernel drivers
   are GPL — read them to *understand behavior*, do not paste code.
3. **Log everything from reconnaissance to `/tmp/m4-recon/`.** File names
   are prescribed below so a future session can pick up cold.
4. **One milestone per phase.** No forward progress until the milestone
   yields a concrete, observable result. Milestones are marked
   "**M#:** ...".
5. **No `sudo` for proxyclient invocations.** The udev rule in §4 gives
   `ahmed` group access to `/dev/ttyACM{0,1}`. `sudo` breaks user Python
   site-packages (`construct`, `pyserial`) and env vars.
6. **Default addresses (change only if reconnaissance contradicts):**

   | Item | Value |
   |---|---|
   | M4 mini static IP (target) | `192.168.0.246` |
   | Comet Lake host IP | `192.168.0.251` |
   | TCP hello port | `3333` |
   | Dockchannel UART base | `0x388128000` |
   | Recon dump directory | `/tmp/m4-recon/` |

   These match `AsahiLinux/m4/.claude/m4_project_context.md:9-10`. If
   Phase 1.6 shows the DHCP-assigned M4 address is now different (e.g.
   the earlier session's `192.168.0.248`), we hard-code the observed one
   in the TCP server and note it in §15.

7. **TCP stack decision:** vendor **lwIP** as a source drop under
   `third_party/lwip/`. Chosen over picoTCP: bigger community, first-class
   raw-API support, tested on bare-metal ARM, MIT license. Trimmed to
   ~4–5 files needed for TCP + ARP + IPv4 + a `netif` glue.

---

<a id="1-ground-truth"></a>
## §1 Ground truth (verified as of 2026-07-07)

Confirmed by the current `m1n1 ed03c49-dirty` boot log and prior recon
in `AsahiLinux/m4/Notes/{1,2,3}.md`.

| Fact | Value | Source of truth |
|---|---|---|
| Machine | M4 Mac mini, Mac16,10 | m1n1 TTY: `Model: Mac16,10` |
| SoC | T8132 (M4) | `AsahiLinux/m4/m1n1/src/soc.h:22, :44` |
| Chip-ID / Board-ID | `0x8132` / `0x2A` | m1n1 TTY boot header |
| CPU part / rev | `0x53` / `0x11` (M4 Donan P) | m1n1 TTY |
| Cores | 10 (E+P) | m1n1 boot log CPU init |
| RAM | 16 GB | m1n1: `mem_size_act: 0x400000000` |
| RAM base (phys_base) | `0x100010f0000` | m1n1 boot_args dump |
| macOS | 15.1 Sequoia (iBoot-11881.41.5) | m1n1 boot log |
| Volume Group UUID | `9C404CDA-E560-4594-A99E-BEBA9587182C` | `Notes/2.md:44` |
| S3C debug UART (pins) | `0x3ad200000` | `soc.h:45` (`EARLY_UART_BASE`) |
| Dockchannel UART (USB) | `0x388128000` | m1n1 boot: `Initialized dockchannel UART at 0x388128000` |
| Dockchannel regs | TX8 `+0x4004`, TX_FREE `+0x4014`, RX8 `+0x401c`, RX_COUNT `+0x402c` | `AsahiLinux/m4/m1n1/src/dockchannel_uart.c:11-14` |
| USB gadget IDs | `1209:316d` "m1n1 uartproxy" | `dmesg` |
| ttyACM0 role | proxy protocol (framed binary) | `cdc_acm 1-8:1.0` |
| ttyACM1 role | raw pass-through (unused for now) | `cdc_acm 1-8:1.2` |
| tty permissions | `crw-rw---- root:uucp 0660` | needs udev rule (§4) |
| MAC address (ethernet0) | in ADT `/chosen`, prop `mac-address-ethernet0` | `AsahiLinux/m4/m1n1/src/kboot.c:765-767, :787-790` |
| Boot policy | Permissive Security, `coih` set — m1n1 enrolled as fuOS | `Notes/2.md`, current behavior |
| USB debug port | rear USB-C port closest to power | verified in current session |

Everything below assumes this state. If a fact drifts (Sonoma update,
disk reflash, cable swap), re-verify before continuing.

**The M4 always answers to power-cycle → m1n1 comes up → USB gadget
enumerates → `ttyACM{0,1}` appear.** That's our substrate.

---

<a id="2-strategy"></a>
## §2 Strategy — kernel as m1n1 `p.call()` payload

### 2.1 Why not raw chainload

Raw chainload (`chainload.py -r -E 0`) replaces m1n1 with our binary,
using `p.reload()`. See `AsahiLinux/m4/m1n1/proxyclient/tools/chainload.py:176-178`.
This is what the current codebase does, and it works — but m1n1's USB-CDC
gadget dies with m1n1, so we lose all observability the moment our kernel
takes over. Every change becomes a coin flip.

### 2.2 What `chainload.py -c` (call mode) gives us

Same file, lines 168-181:

```python
if args.call:
    print(f"Shutting down MMU...")
    try:
        p.mmu_shutdown()
    except ProxyCommandError:
        pass
    print(f"Jumping to stub at 0x{stub.addr:x}")
    p.call(stub.addr, ..., reboot=True)
else:
    p.reload(stub.addr, ...)

iface.nop()
print("Proxy is alive again")   # <-- key line
```

**Call mode invokes our code via `P_CALL` (opcode 2, `proxy.h:11`),
m1n1's MMU is disabled first, but m1n1 itself remains loaded in memory
and the proxy loop resumes when our code returns.** USB stays alive
because the CDC-ACM controller keeps its FIFOs; m1n1 restarts polling
after we return.

### 2.3 The interaction model

```
   Comet Lake host                              M4 (running m1n1 + our payload)
   ─────────────────                            ────────────────────────────────
   shell.py session                             m1n1 in uartproxy_run()
       │                                          │
       │  p.pcie_init()                           ├─ pcie bring-up (both GE + main)
       │  p.dart_init(...)                        ├─ program DART SIDs
       │  p.dart_map(...)                         ├─ install IOMMU translations
       │  p.malloc(size)   ────────>  addr        ├─ heapblock_alloc
       │  p.memcpy8(addr, src, len)               ├─ copy kernel.bin to addr
       │  p.dc_cvau/ic_ivau(addr, len)            ├─ flush caches
       │  p.call(addr + entry_offset,             ├─ [OUR CODE RUNS HERE]
       │         nic_mmio, dart_iova, ba)         │    - init NIC
       │                                          │    - service ARP/IP/TCP loop
       │                                          │    - eventually RET to m1n1
       │  <────  return value in x0               ├─ proxy resumes
       │  ...next iteration                       │
```

Our C code runs at EL2 with m1n1's environment: dockchannel UART already
programmed (we can write to `0x388128000` directly for `printf`), heap
already sized, ADT parsed, IRQs off (fine — we poll), MMU off (fine —
we use physical addresses everywhere), stack provided by m1n1 (the
`p.call` machinery gives us one).

### 2.4 What "returning" means

For iteration we prefer short calls: `hello_run(state)` runs the NIC
polling loop for N iterations or M milliseconds, returns; Python calls
it again. Between calls, m1n1's proxy runs, USB stays lively, we can
`print()` state from Python.

For the final "server that answers TCP forever" demo we can either:

- (a) call `hello_run()` in a Python loop that never exits — control
  bounces host↔M4 every few ms; simple, and Ctrl-C on the host halts
  it cleanly.
- (b) commit to a single long `p.call()` that never returns and hangs
  in a polling loop. m1n1's USB TX FIFO backs up over minutes; fine
  for a short demo, not for indefinite use.

Default: (a). Switch to (b) only if latency is a problem.

### 2.5 Non-goals of the payload

We are **not** going to:

- Initialize our own MMU. m1n1 shuts its MMU off before `p.call()`
  (`chainload.py:171`). We run identity-mapped physical the whole way.
- Bring up our own USB. m1n1's USB is our console, indefinitely.
- Rewrite `p.pcie_init()` / DART / SMC. m1n1 already does these.
- Support macOS coexistence at runtime. This kernel does not persist —
  every boot starts m1n1 fresh, and we re-upload.

---

<a id="3-reference-map"></a>
## §3 Reference map (every AsahiLinux file:line we depend on)

### 3.1 m1n1 sources — read for understanding, do not vendor

| File | Lines | What we get |
|---|---|---|
| `AsahiLinux/m4/m1n1/src/soc.h` | 22, 44-45 | `T8132 = 0x8132`, `EARLY_UART_BASE = 0x3ad200000` |
| `AsahiLinux/m4/m1n1/src/dockchannel_uart.c` | 11-14, 35-44 | Register layout + polled TX loop we mimic |
| `AsahiLinux/m4/m1n1/src/proxy.h` | 8-178 | Every `P_*` opcode we'll call from Python |
| `AsahiLinux/m4/m1n1/src/proxy.c` | 41-411 | Server-side dispatch — read to understand semantics |
| `AsahiLinux/m4/m1n1/src/pcie.c` | 227-269, 725-739 | `pcie_init_controller` compat detection; three RCs to bring up |
| `AsahiLinux/m4/m1n1/src/pcie.c` | 620-720 | Per-port link-training + register writes we might have to fork |
| `AsahiLinux/m4/m1n1/src/kboot.c` | 754-810 | MAC-address extraction pattern (mirror this in Python) |
| `AsahiLinux/m4/m1n1/src/nvme.c` | 291-400 | PCIe device init template (get MMIO base from ADT + queue setup) |
| `AsahiLinux/m4/m1n1/src/main.c` | 141-209 | `m1n1_main` → `run_actions` → `uartproxy_run` — the loop we return to |
| `AsahiLinux/m4n1/src/pcie.c` | 258-286 | Adds `apcie,t8122`, `apcie,t6030`, `apcie,t6031` — pattern to copy if T8132 is unlisted |

### 3.2 m1n1 proxyclient tools — invoked directly

| File | Lines | Use |
|---|---|---|
| `AsahiLinux/m4/m1n1/proxyclient/tools/chainload.py` | 11, 168-181 | `-c` call mode |
| `AsahiLinux/m4/m1n1/proxyclient/tools/shell.py` | full file | Interactive REPL (`u`, `p`, `iface` objects live here) |
| `AsahiLinux/m4/m1n1/proxyclient/tools/run_guest.py` | full file | Not used yet (HV mode); note for future observability upgrade |
| `AsahiLinux/m4/m1n1/proxyclient/m1n1/proxy.py` | 705, 783, 802, 891, 903, 1000, 1051, 1116 | `p.call`, `p.write32`, `p.read32`, `p.memcpy64/8`, `p.malloc`, `p.dart_init`, `p.pcie_init` |
| `AsahiLinux/m4/m1n1/proxyclient/m1n1/proxyutils.py` | 43, 131, 141, 165, 174, 186, 235, 253 | `u` helper — `u.mrs()`, `u.msr()`, `u.exec()`, `u.compressed_writemem()`, `u.adt` |
| `AsahiLinux/m4/m1n1/proxyclient/experiments/pcie_enable_devices.py` | full file (18 lines) | SMC key to power PCIe devices before `p.pcie_init()` |
| `AsahiLinux/m4/m1n1/proxyclient/experiments/dart_dump.py` | full file (19 lines) | `DART.from_adt(u, ...).dump_all()` — dump L1/L2 tables |
| `AsahiLinux/m4/m1n1/proxyclient/experiments/mmio_sweep.py` | full file | Address probing when spelunking undocumented MMIO |
| `AsahiLinux/m4/m1n1/proxyclient/m1n1/hw/dart.py` | class `DART` | Programmatic DART wrapper (init, map, unmap, dump) |

### 3.3 Prior recon notes — reread first

| File | Lines | What it covers |
|---|---|---|
| `AsahiLinux/m4/Notes/1.md` | full | System identity, security state, disk layout |
| `AsahiLinux/m4/Notes/2.md` | full | 1TR entry, bputil, kmutil, enrollment procedure |
| `AsahiLinux/m4/Notes/3.md` | full | Deployment steps, UART discussion, mistakes we made |
| `AsahiLinux/m4/.claude/m4_project_context.md` | 8-49 | The prior IP + strategy context |
| `AppleSiliconM4/PLAN.md` | 0-131 | The UDP/chainload plan; §0 ground-truth table |
| `AppleSiliconM4/README.md` | 12-63 | The current kernel's structure — is what we're refactoring |

### 3.4 lwIP — external, to be fetched during Phase 7

| Source | Version | Layout |
|---|---|---|
| `git clone https://git.savannah.nongnu.org/git/lwip.git` | STABLE-2_2_0_RELEASE | Only need `src/core/{init,tcp,ip4,inet_chksum,memp,mem,pbuf}.c` + `src/core/ipv4/*` + `src/netif/ethernet.c` — copy under `third_party/lwip/` |

### 3.5 Datasheets — obtain during Phase 1.6 once NIC is identified

Placeholders — filled in after `vendor:device` comes from PCI config space:

- If NIC is Broadcom BCM57xxx family: `tg3` Linux driver as reference,
  Broadcom NetXtreme programmer's manual (public PDF).
- If Aquantia AQC113: Linux `drivers/net/ethernet/aquantia/atlantic/`.
- If Realtek RTL8125: `r8169` Linux driver; datasheet public.
- If Apple's own SoC-integrated ethernet: no docs, needs RE via
  MMIO sweep + Linux `apple-*` driver reading. This is the doomsday scenario;
  see §14.

---

<a id="4-environment-prep"></a>
## §4 Environment prep on the Comet Lake dev host

Do these once. They fix pain points we already burnt on:

### 4.1 udev rule for m1n1 gadget

Create `/etc/udev/rules.d/70-m1n1.rules`:

```
# m1n1 uartproxy CDC-ACM composite device
SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="316d", \
    ATTRS{bInterfaceNumber}=="00", MODE="0660", GROUP="uucp", SYMLINK+="m1n1"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="316d", \
    ATTRS{bInterfaceNumber}=="02", MODE="0660", GROUP="uucp", SYMLINK+="m1n1-raw"
```

Then:

```bash
sudo udevadm control --reload
sudo udevadm trigger
sudo usermod -aG uucp ahmed    # once, followed by re-login or `newgrp uucp`
```

Result: `/dev/m1n1` → the proxy interface; `/dev/m1n1-raw` → the raw
UART pass-through. `shell.py` and `chainload.py` default to `/dev/m1n1`
per `AsahiLinux/m4/m1n1/proxyclient/m1n1/proxy.py:134` — no `M1N1DEVICE`
env var needed.

### 4.2 Python deps

```bash
sudo pacman -S python-construct python-pyserial
# already have python-construct as of 2026-07-07
```

Verify: `python3 -c 'import construct, serial; print("ok")'`.

### 4.3 Serial monitor

For watching `/dev/m1n1-raw` (kernel dockchannel output) while running
scripts on the proxy channel:

```bash
sudo pacman -S picocom      # ~200 KB; single-binary; clean UX
```

Fallback with zero install: `python3 -m serial.tools.miniterm /dev/m1n1-raw 115200`.

### 4.4 Recon output dir

```bash
mkdir -p /tmp/m4-recon
```

Everything in Phase 1 lands here.

### 4.5 Repository layout for this project

Extend the current `AppleSiliconM4/` tree:

```
AppleSiliconM4/
├── PLAN.md                 # historical, do not modify
├── PLAN_2.md               # this document
├── README.md               # current; will be revised at end of Phase 2
├── Makefile
├── linker.ld               # will change: relocatable ELF for payload use
├── include/
│   ├── types.h
│   ├── kernel.h
│   ├── boot_args.h
│   ├── dockchannel.h       # NEW — replaces uart.h
│   ├── nic.h               # NEW — Phase 5
│   ├── arp.h               # NEW — Phase 6
│   └── net.h               # NEW — shared IP/MAC constants
├── src/
│   ├── start.S             # simplified entry for p.call()
│   ├── vectors.S           # stays as-is (fatal handlers)
│   ├── panic.c             # switch UART calls to dockchannel
│   ├── dockchannel.c       # NEW
│   ├── main.c              # kmain becomes kentry(u64 nic_base, u64 dma_iova, void *ba)
│   ├── nic_<chip>.c        # NEW — Phase 5, chip suffix filled in after §7
│   ├── arp.c               # NEW
│   └── net.c               # NEW — MAC/IP config, checksum helpers
├── third_party/
│   └── lwip/               # NEW — Phase 7, drop-in from lwip release tarball
│       ├── src/core/{...}.c
│       ├── src/netif/ethernet.c
│       └── include/lwip/{...}.h
├── scripts/                # NEW — Python from Comet Lake
│   ├── recon.py            # Phase 1 — reads ADT, dumps everything to /tmp/m4-recon
│   ├── upload_and_call.py  # Phase 2 — the iteration harness
│   ├── pcie_up.py          # Phase 3 — bring up PCIe + enumerate BARs
│   ├── dart_up.py          # Phase 4 — configure DART for NIC
│   └── run_hello.py        # Phase 8 — the final glue
└── build/
    └── (generated)
```

`src/uart.{c,h}` gets deleted. It targets the wrong UART.

---

<a id="5-phase-1--reconnaissance"></a>
## §5 Phase 1 — Reconnaissance (dump everything to disk)

**Objective:** answer four questions with files on disk that a future
session can read cold, without touching the M4 again.

1. What is the ADT compatible string for M4's PCIe root(s)?
2. What is the NIC's PCIe vendor:device ID?
3. What is the M4's assigned MAC address?
4. Which DART instance serves the NIC's DMA?

**Deliverable:** `/tmp/m4-recon/` populated per the file list below, plus
`recon-summary.md` (hand-written after) that captures the four answers.

### 5.1 Power-cycle the M4 into m1n1

```bash
# On M4: quick press of power button after long-press-to-off
# Wait for /dev/m1n1 to appear in dmesg (~15s)
ls -l /dev/m1n1 /dev/m1n1-raw
```

### 5.2 Capture m1n1 boot log

Two options — pick either.

Option A (background reader keeps running):

```bash
picocom -b 115200 --logfile /tmp/m4-recon/m1n1-boot.log /dev/m1n1-raw
# Ctrl-A Ctrl-X to quit; log persists.
```

Option B (one-shot capture without a live terminal):

```bash
timeout 3 cat /dev/m1n1-raw > /tmp/m4-recon/m1n1-boot.log
```

Repeat after each power cycle.

### 5.3 Interactive m1n1 shell — dump ADT

```bash
cd /home/ahmed/Projects/AsahiLinux/m4/m1n1/proxyclient
./tools/shell.py
```

Inside the shell (`u`, `p`, `iface` in scope):

```python
# Full ADT walk to text
with open("/tmp/m4-recon/adt.txt", "w") as f:
    f.write(str(u.adt))

# Raw ADT bytes
with open("/tmp/m4-recon/adt.bin", "wb") as f:
    f.write(u.adt.build())

# ADT children directly under /arm-io — inventory of on-die peripherals
with open("/tmp/m4-recon/arm-io-children.txt", "w") as f:
    for node in u.adt["/arm-io"]:
        f.write(f"{node._name}\n")
```

### 5.4 Locate PCIe root complexes and record compatible strings

```python
# From shell.py
def dump_pcie_node(path):
    try:
        node = u.adt[path]
    except KeyError:
        return None
    props = {}
    for name in dir(node):
        if name.startswith("_"):
            continue
        try:
            v = getattr(node, name)
            props[name] = v
        except Exception:
            pass
    return props

pcie_nodes = {}
for path in ["/arm-io/apcie", "/arm-io/apcie-ge0", "/arm-io/apcie-ge1"]:
    pcie_nodes[path] = dump_pcie_node(path)

with open("/tmp/m4-recon/pcie-nodes.txt", "w") as f:
    for path, props in pcie_nodes.items():
        f.write(f"=== {path} ===\n")
        if props is None:
            f.write("NOT PRESENT\n\n")
            continue
        f.write(f"compatible: {props.get('compatible')}\n")
        f.write(f"reg (raw): {props.get('reg')}\n")
        f.write(f"lane-cfg: {props.get('lane_cfg', '(none)')}\n\n")
```

**Decision made from this file:**
- If `compatible` matches one of `apcie,t8103`, `apcie,t6000`, `apcie,t8112`,
  `apcie,t6020`, `apcie-ge,t6020`, `apcie,t8122`, `apcie,t6030`, `apcie,t6031`
  → `p.pcie_init()` will work. Proceed to §7A.
- Otherwise (likely `apcie,t8132`) → we must patch m1n1's `src/pcie.c`
  first. Proceed to §7B.

### 5.5 Extract the ethernet MAC from `/chosen`

Pattern mirrors `AsahiLinux/m4/m1n1/src/kboot.c:754-810`.

```python
chosen = u.adt["/chosen"]
mac_eth0 = getattr(chosen, "mac_address_ethernet0", None)
mac_eth1 = getattr(chosen, "mac_address_ethernet1", None)

with open("/tmp/m4-recon/mac-address.txt", "w") as f:
    f.write(f"ethernet0: {mac_eth0!r}\n")
    f.write(f"ethernet1: {mac_eth1!r}\n")
```

If both are `None`, the ADT key names differ on this SoC; grep the raw
ADT dump for `mac-address` and record every match.

### 5.6 Attempt PCIe bring-up and enumerate config space

**Only do this after §5.4 confirms compatible support**, otherwise `p.pcie_init()`
will fail visibly (harmless but noisy).

First power the PCIe devices (mirrors `experiments/pcie_enable_devices.py`):

```python
from m1n1.fw.smc import SMCClient
smc_addr = u.adt["arm-io/smc"].get_reg(0)[0]
smc = SMCClient(u, smc_addr, None)
smc.start()
smc.start_ep(0x20)
smc.smcep.write32("gP0d", 0x800001)   # power apcie
smc.smcep.write32("gP1a", 1)          # ge0
# May need gP1b (ge1) too — try if only 2 controllers come up
smc.stop()
```

Bring up PCIe:

```python
rc = p.pcie_init()
# Expect 0 on success; -1 on unsupported compat
```

Enumerate ECAM (config space is memory-mapped starting at
`0x600000000` on t8103/t8112/t8122; base for T8132 comes from ADT
`/arm-io/apcie` reg[0]):

```python
# ecam_base + (bus << 20) + (dev << 15) + (fn << 12) + reg
ecam_base = u.adt["/arm-io/apcie"].get_reg(0)[0]

with open("/tmp/m4-recon/pcie-config-space.txt", "w") as f:
    for bus in range(4):
        for dev in range(32):
            for fn in range(8):
                cfg = ecam_base + (bus << 20) + (dev << 15) + (fn << 12)
                vid_did = p.read32(cfg + 0x00)
                if vid_did in (0xffffffff, 0x00000000):
                    if fn == 0:
                        break   # no device, skip whole slot
                    continue
                vid = vid_did & 0xffff
                did = (vid_did >> 16) & 0xffff
                cls = p.read32(cfg + 0x08) >> 8   # class code, 24-bit
                f.write(f"{bus:02x}:{dev:02x}.{fn} "
                        f"VID={vid:04x} DID={did:04x} CLS={cls:06x}\n")
                # Dump full 256-byte config header for later reference
                for reg in range(0, 0x100, 4):
                    val = p.read32(cfg + reg)
                    f.write(f"    +0x{reg:03x} = 0x{val:08x}\n")
```

Repeat with `ecam_base` from `/arm-io/apcie-ge0` and `-ge1` if they exist,
writing to `pcie-config-space-ge0.txt` / `-ge1.txt`.

### 5.7 Identify the NIC

Any device with class code `0x02xxxx` is an Ethernet controller.
Look for that in the outputs and record vendor:device to
`/tmp/m4-recon/nic-identity.txt`:

```
BDF: bb:dd.f
Vendor:  0xXXXX  (lookup: <name>)
Device:  0xYYYY  (lookup: <name>)
Class:   0x020000 (Ethernet)
BAR0:    0x????????  (from config +0x10)
BAR1..5: (as extracted)
```

Vendor ID lookup: `https://pcisig.com/membership/member-companies` (or
the `pci-ids` package if installed — `pacman -S hwids`, then
`grep -w <vid> /usr/share/hwdata/pci.ids`).

### 5.8 Locate the DART for the NIC

The NIC lives under one of the apcie controllers; its DART is usually
`/arm-io/dart-apcie` or a per-port variant. From shell:

```python
darts = []
for name in u.adt["/arm-io"]:
    if name._name.startswith("dart-"):
        darts.append(name._name)
with open("/tmp/m4-recon/darts.txt", "w") as f:
    for d in darts:
        f.write(f"{d}\n")
```

For the specific DART we'll want, `dart_dump.py` gives us initial state:

```bash
./tools/shell.py -c 'exec(open("experiments/dart_dump.py").read())' dart-apcie
# Or just import DART and call dump_all(); output goes to stdout, tee to file.
```

Redirect stdout to `/tmp/m4-recon/dart-apcie.txt`.

### 5.9 Write `recon-summary.md`

Hand-written distillation, 40-80 lines. Must answer:

1. PCIe compat for T8132? (§5.4)
2. NIC vendor:device? (§5.7)
3. NIC BAR0 (MMIO base after PCIe enumeration)?
4. MAC address?
5. DART node name?
6. Any surprises (e.g., PCIe bring-up crashed, m1n1 output showed errors)?

**M1 (Milestone 1):** `/tmp/m4-recon/recon-summary.md` exists and answers
questions 1–5 with concrete values.

---

<a id="6-phase-2--kernel-as-payload"></a>
## §6 Phase 2 — Rebuild the kernel as a `p.call()` payload

**Objective:** replace the current chainload-oriented kernel with a
callable function that prints "Hello World" via the dockchannel UART
while m1n1 is still up.

### 6.1 New dockchannel driver

Delete `src/uart.c` and `include/uart.h`. Add:

`include/dockchannel.h`:

```c
#ifndef DOCKCHANNEL_H
#define DOCKCHANNEL_H
#include "types.h"

#define DOCKCHANNEL_BASE_T8132  0x388128000ULL
#define DOCKCHANNEL_TX8         0x4004
#define DOCKCHANNEL_TX_FREE     0x4014
#define DOCKCHANNEL_RX8         0x401c
#define DOCKCHANNEL_RX_COUNT    0x402c

void dc_putc(char c);
void dc_puts(const char *s);
void dc_puthex64(u64 v);
void dc_puthex32(u32 v);
void dc_putdec(u64 v);
#endif
```

`src/dockchannel.c` — polling TX, 40 lines. Mirror
`AsahiLinux/m4/m1n1/src/dockchannel_uart.c:35-44`:

```c
static void dc_putbyte(u8 c)
{
    volatile u32 *tx_free = (volatile u32 *)(DOCKCHANNEL_BASE_T8132 + DOCKCHANNEL_TX_FREE);
    volatile u32 *tx_data = (volatile u32 *)(DOCKCHANNEL_BASE_T8132 + DOCKCHANNEL_TX8);
    while (*tx_free == 0) {}
    *tx_data = c;
}

void dc_putc(char c)
{
    if (c == '\n') dc_putbyte('\r');
    dc_putbyte(c);
}

/* dc_puts, dc_puthex*, dc_putdec are trivial wrappers */
```

No init — m1n1 already programmed the hardware, and its state persists
across our `p.call()` because m1n1 is still resident. Verify:

- `AsahiLinux/m4/m1n1/src/dockchannel_uart.c:22-30` — m1n1 reads
  `/arm-io/dockchannel-uart` reg from ADT (dynamic base). For our M4
  boot logs it resolves to `0x388128000`. If a future SoC differs, we
  can pass the actual base as an arg to `kentry` from Python.

### 6.2 Reduce entry to a single callable

Rewrite `src/start.S` to be a leaf function that establishes just enough
state to call C, and returns:

```asm
    .global _kentry
    .type _kentry, %function
_kentry:
    /* x0-x2 = args from Python (nic_base, dma_iova, boot_args_ptr) */
    /* On entry: caller's SP is valid (m1n1 provides one); x30 = LR = return address */
    stp     x29, x30, [sp, #-16]!
    mov     x29, sp
    bl      kmain
    /* x0 already holds return code */
    ldp     x29, x30, [sp], #16
    ret
```

No more `park_secondaries`, no more BSS zeroing, no more setting `VBAR_EL2`.
m1n1 owns those. Our kernel is a plain leaf function.

`src/main.c` — `kmain` becomes:

```c
int kmain(u64 nic_mmio, u64 dma_iova, void *ba)
{
    dc_puts("\n[kernel] hello from bare-metal M4 payload\n");
    dc_puts("[kernel] nic_mmio = "); dc_puthex64(nic_mmio); dc_putc('\n');
    dc_puts("[kernel] dma_iova = "); dc_puthex64(dma_iova); dc_putc('\n');
    /* Phase 2 milestone stops here. Later phases extend this. */
    return 0;
}
```

### 6.3 Linker script for relocatable payload

`linker.ld` needs to produce a position-independent flat blob. Two rules:

1. Everything is at offsets from `0`; the load address is decided at
   runtime by `p.malloc()` on the host side.
2. `_kentry` is at file offset `0` so we can pass the raw upload address
   to `p.call()` unchanged.

```
ENTRY(_kentry)
SECTIONS {
    . = 0;
    .text : { *(.text._kentry) *(.text*) }
    .rodata : { *(.rodata*) }
    .data : { *(.data*) }
    .bss (NOLOAD) : { *(.bss*) *(COMMON) }
}
```

No `.bss` in the raw binary; if any C code uses uninitialized globals,
the caller must have zeroed the region (Python does this after `malloc`).

### 6.4 Upload + call harness

`scripts/upload_and_call.py`:

```python
#!/usr/bin/env python3
import sys, pathlib, struct
sys.path.append("/home/ahmed/Projects/AsahiLinux/m4/m1n1/proxyclient")
from m1n1.setup import *

kernel = pathlib.Path(sys.argv[1]).read_bytes()
size = len(kernel)
addr = p.memalign(0x4000, size)   # 16 KiB align
iface.writemem(addr, kernel)      # or u.compressed_writemem for speed
p.dc_cvau(addr, size)
p.ic_ivau(addr, size)
print(f"[host] uploaded {size} bytes to 0x{addr:x}")
ret = p.call(addr, 0, 0, 0)       # nic_mmio, dma_iova, ba placeholders
print(f"[host] kmain returned 0x{ret:x}")
```

### 6.5 Makefile — split targets

Two artifacts, one build system:

```
build/kernel.bin         # raw flat, for p.call() upload
build/kernel.elf         # for objdump / addr2line debugging
```

Reuse the existing `Makefile` structure; add a `payload.bin` target that
strips headers and pads to 16 B, and drop the `chainload` target we no
longer use.

### 6.6 Milestone

**M2:** From the Comet Lake host:

```
python3 scripts/upload_and_call.py build/kernel.bin
```

...and observe on `/dev/m1n1-raw` (via picocom in a second terminal):

```
[kernel] hello from bare-metal M4 payload
[kernel] nic_mmio = 0x0
[kernel] dma_iova = 0x0
```

Iteration loop is now: edit C → `make` → `python3 upload_and_call.py …`
→ read serial. Expected round-trip: ~2 seconds. This is our new baseline.

---

<a id="7-phase-3--pcie-reach"></a>
## §7 Phase 3 — PCIe reach (surface the NIC)

Split into 7A (m1n1 already supports M4 PCIe) and 7B (must patch m1n1).
Take the branch chosen by §5.4.

### §7A — If §5.4 shows a supported compat string

`scripts/pcie_up.py`:

```python
#!/usr/bin/env python3
import sys, pathlib
sys.path.append("/home/ahmed/Projects/AsahiLinux/m4/m1n1/proxyclient")
from m1n1.setup import *
from m1n1.fw.smc import SMCClient

# Power the PCIe controllers via SMC (mirrors experiments/pcie_enable_devices.py)
smc = SMCClient(u, u.adt["arm-io/smc"].get_reg(0)[0], None)
smc.start(); smc.start_ep(0x20)
smc.smcep.write32("gP0d", 0x800001)
smc.smcep.write32("gP1a", 1)
smc.stop()

# Bring up PCIe
assert p.pcie_init() == 0, "pcie_init failed"

# Enumerate — find the ethernet controller (class = 0x02)
ECAM = u.adt["/arm-io/apcie-ge0"].get_reg(0)[0]  # or /apcie / /apcie-ge1 per recon
for dev in range(32):
    cfg = ECAM + (dev << 15)
    vd = p.read32(cfg + 0x00)
    if vd == 0xffffffff:
        continue
    cls = p.read32(cfg + 0x08) >> 24
    if cls != 0x02:
        continue
    # Found the NIC
    bar0 = p.read32(cfg + 0x10) & ~0xf
    bar1 = p.read32(cfg + 0x14)
    if (bar0 & 0x7) == 0x4:   # 64-bit BAR
        bar0 |= bar1 << 32
    # Enable memory space + bus master
    cmd = p.read32(cfg + 0x04)
    p.write32(cfg + 0x04, cmd | 0x6)
    print(f"NIC at BDF 00:{dev:02x}.0, BAR0 = 0x{bar0:x}")
    break
```

### §7B — Adding `apcie,t8132` to m1n1

Read `AsahiLinux/m4n1/src/pcie.c:258-286` for the pattern used when
`apcie,t8122` was added. Apply the same three-step change to
`AsahiLinux/m4/m1n1/src/pcie.c`:

1. Add a `else if (adt_is_compatible(adt, adt_offset, "apcie,t8132"))`
   clause after line 269, cloning the `apcie,t8122` block (fuse_bits
   likely `NULL`, `pcie_regs = &regs_t602x`, no lane-cfg fanout).
2. `pcie_init_controller` should behave the same as t8122/t6020 unless
   we discover otherwise. If the first attempt hangs at link training,
   check tunables (`pcie-rc-tunables`, `pcie-rc-gen4-shadow-tunables`)
   in the ADT.
3. Rebuild `m1n1.macho` per `Notes/3.md` (BRINGUP config, cargo/rustc
   from rustup), re-enroll via `kmutil configure-boot`.

There is a real risk that M4 PCIe needs new tunables m1n1 doesn't apply
correctly. Mitigation: monitor `pcie: Port failed to become idle`
messages in `/tmp/m4-recon/m1n1-boot.log`.

### 7.3 Milestone

**M3:** `pcie_up.py` prints the NIC's BDF and BAR0. On a following
`p.read32(bar0 + 0x00)` call, we get a plausible non-`0xffffffff` value
(usually the NIC's chip revision or a well-known register). Save that
address as `nic_mmio` for Phase 4.

---

<a id="8-phase-4--dart-for-nic"></a>
## §8 Phase 4 — DART for the NIC (DMA path)

DART is Apple's IOMMU. Without a mapping, any DMA the NIC attempts
faults into `dart interrupt` and the NIC hangs.

### 8.1 Understand what m1n1 gives us

`AsahiLinux/m4/m1n1/proxyclient/m1n1/hw/dart.py` (class `DART`) has:

- `DART.from_adt(u, "arm-io/dart-apcie")` — instantiates from ADT.
- `.iomap(size)` — pick a free IOVA range.
- `.iomap_at(iova, addr, size)` — map an existing physical range.
- `.dump_all()` — for troubleshooting.

For our purposes we want to allocate a physically contiguous block via
`p.memalign()`, then `dart.iomap_at()` it. The NIC's stream ID (SID) is
usually 0 for a single-function NIC on a per-device DART; if the DART is
shared, the SID comes from the PCIe RC's routing table.

### 8.2 `scripts/dart_up.py`

```python
#!/usr/bin/env python3
import sys, pathlib
sys.path.append("/home/ahmed/Projects/AsahiLinux/m4/m1n1/proxyclient")
from m1n1.setup import *
from m1n1.hw.dart import DART

DART_NODE = sys.argv[1] if len(sys.argv) > 1 else "arm-io/dart-apcie"
STREAM_ID = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0

dart = DART.from_adt(u, DART_NODE)
dart.initialize()
print("DART regs before mapping:"); dart.dart.regs.dump_regs()

# Allocate a 128 KiB DMA buffer aligned to 16 KiB
BUF_SIZE = 128 * 1024
phys = p.memalign(0x4000, BUF_SIZE)
iova = dart.iomap_at(STREAM_ID, 0x40000, phys, BUF_SIZE)   # arbitrary IOVA base
print(f"Allocated phys = 0x{phys:x}, mapped at iova = 0x{iova:x}")

# Save the pair for Phase 5
with open("/tmp/m4-recon/nic-dma.txt", "w") as f:
    f.write(f"dart_node={DART_NODE}\nsid={STREAM_ID}\nphys=0x{phys:x}\niova=0x{iova:x}\nsize=0x{BUF_SIZE:x}\n")
```

### 8.3 Sanity check — no DMA yet, but verify the mapping

Write a magic value to the physical buffer, read it back through the
IOVA path via a synthesized CPU access (CPU sees the physical directly
anyway; DART only translates NIC-side accesses). The real DMA test comes
in Phase 5.

Alternate sanity: dump the DART L1/L2 tables after mapping and confirm
our IOVA range shows up:

```python
dart.dump_all()
```

### 8.4 Milestone

**M4:** `nic-dma.txt` written with a `(phys, iova)` pair that survives
`dart.dump_all()` verification. The NIC has not been touched yet.

---

<a id="9-phase-5--nic-driver"></a>
## §9 Phase 5 — NIC driver (bare-metal C)

**Bounded scope: RX + TX rings + link status + MAC address program.
No offload, no MSI, no interrupts, no ring resize.**

The driver code is chip-specific. Fill in `<chip>` after §5.7 gives us
the vendor:device.

### 9.1 Skeleton `include/nic.h`

```c
#ifndef NIC_H
#define NIC_H
#include "types.h"

#define NIC_MAX_FRAME 1518
#define NIC_RING_LEN  16

struct nic_frame {
    u16 len;
    u8  data[NIC_MAX_FRAME];
};

int  nic_init(u64 mmio_base, u64 dma_iova, u8 mac[6]);
int  nic_rx(struct nic_frame *out);   /* returns 0 on empty, len on success */
int  nic_tx(const void *frame, u32 len);
void nic_link_status(void);           /* prints via dockchannel */
#endif
```

### 9.2 What every NIC driver has to do

Regardless of chip:

1. Reset the controller (chip-specific magic write to a control reg).
2. Program the MAC address into the RX filter.
3. Set up an RX descriptor ring in the DMA buffer we mapped in §8.
4. Set up a TX descriptor ring in the same buffer at a different offset.
5. Enable RX and TX, clear stats.
6. Poll RX head vs. tail, copy incoming frame out; write descriptors back.
7. On TX, copy frame in, write TX head, tell hardware to fire.

Line count expectation: 400-800 LOC for a competent basic driver on any
of these chips. Not fun, but bounded.

### 9.3 Chip-specific reference paths

Fill after §5.7:

- **Broadcom BCM57xxx (`tg3` family):**
  Linux `drivers/net/ethernet/broadcom/tg3.c`. Basic ring init lives in
  `tg3_init_hw`/`tg3_rx_prodring_alloc`. Datasheet: NetXtreme
  programmer's guide (public).
- **Realtek RTL8125:** Linux `drivers/net/ethernet/realtek/r8169_main.c`
  — RX/TX descriptor formats are well documented in the driver comments.
- **Aquantia AQC113:** Linux `drivers/net/ethernet/aquantia/atlantic/`.
  MMIO-heavy, register defs in `aq_hw_atl_a0_internal.h`.
- **Apple SoC-integrated (worst case):** no public docs. See §14.

### 9.4 First-shot smoke test — link status only

Before rings, just read the link status register and print it. Every
NIC exposes one; find it from the datasheet. Update `kmain`:

```c
int kmain(u64 nic_mmio, u64 dma_iova, void *ba)
{
    dc_puts("[kernel] nic init\n");
    u8 mac[6] = { 0x02, 'M', '4', 'D', 'E', 'V' };   /* overwritten later from ADT */
    if (nic_init(nic_mmio, dma_iova, mac) < 0) {
        dc_puts("[kernel] nic_init failed\n"); return -1;
    }
    nic_link_status();   /* prints "link up / 1G / full" etc. */
    return 0;
}
```

### 9.5 First real milestone — send a broadcast frame

Build a minimal Ethernet frame (14-byte header + 46 bytes zero payload)
addressed to `ff:ff:ff:ff:ff:ff`. Call `nic_tx()`. On the Comet Lake host,
in a separate terminal:

```bash
sudo tcpdump -i eth0 -e -n -c 5 ether host <M4_MAC>
```

Expected: tcpdump prints one broadcast frame with our source MAC.

**M5:** `tcpdump` shows the frame. If nothing appears, sequence:
1. Confirm link is up (nic_link_status output).
2. Confirm the NIC's TX queue head advanced (poll from Python after
   returning from `kmain`).
3. Check DART didn't fault (`dart.dump_all()` shows fault registers).

---

<a id="10-phase-6--arp"></a>
## §10 Phase 6 — ARP (be discoverable)

lwIP has ARP built in, but the state machine is a fine test that our
NIC RX path works, and being discoverable via `arping` is a useful
"halfway" milestone before pulling in a full TCP stack.

### 10.1 Static IP + MAC configuration

`include/net.h`:

```c
#define MY_MAC { /* from /tmp/m4-recon/mac-address.txt */ }
#define MY_IPV4 { 192, 168, 0, 246 }
```

If the LAN uses DHCP and `192.168.0.246` is taken by macOS-M4 on the
same MAC, we have three options:
- (a) Use a distinct IP outside the DHCP pool (e.g. `192.168.0.99`).
- (b) Alter the MAC to a locally-administered one (`0x02` prefix) so
  the DHCP server treats it as a new host.
- (c) Do DHCP client in lwIP later.

Default: (a), because it's simplest and doesn't affect macOS boots.
Note the chosen IP in `recon-summary.md`.

### 10.2 ARP handler — `src/arp.c`

Handle both directions:

- **Reply:** received frame with EtherType `0x0806`, opcode `0x0001`,
  target IP == ours → build reply with opcode `0x0002`, swap fields,
  send with `nic_tx`.
- **Request:** if we need to resolve a peer's MAC (e.g. when sending an
  IP packet), build request; wait for reply frame in poll loop.

~150 lines of C. Struct layout:

```c
struct __attribute__((packed)) arp_pkt {
    u16 htype, ptype;
    u8  hlen, plen;
    u16 opcode;
    u8  sender_mac[6]; u8 sender_ip[4];
    u8  target_mac[6]; u8 target_ip[4];
};
```

### 10.3 Kernel main loop

`kmain` becomes a bounded poll loop that returns after N iterations:

```c
int kmain(u64 nic_mmio, u64 dma_iova, void *ba)
{
    u8 mac[6] = MY_MAC;
    u8 ip[4]  = MY_IPV4;
    nic_init(nic_mmio, dma_iova, mac);
    struct nic_frame f;
    for (int i = 0; i < 10000; i++) {
        int n = nic_rx(&f);
        if (n <= 0) continue;
        if (is_arp_request_for(&f, ip))
            send_arp_reply(&f, mac, ip);
    }
    return 0;
}
```

10 000 iterations at ~1 µs each = 10 ms max blocking. Python calls in a
loop; between calls, m1n1 services its USB proxy.

### 10.4 Milestone

**M6:** From Comet Lake, `arping -c 3 -I eth0 192.168.0.246` (or the
IP we chose) receives replies within seconds. Then:

```
ip neigh show 192.168.0.246
```

...shows the M4's MAC. We are now discoverable on the LAN.

---

<a id="11-phase-7--lwip"></a>
## §11 Phase 7 — lwIP integration

lwIP's raw API (`tcp_new`, `tcp_bind`, `tcp_listen`, `tcp_accept`) is
callback-driven and does not need threads. It fits bare-metal perfectly.

### 11.1 Vendor lwIP

```bash
git clone --depth=1 -b STABLE-2_2_0_RELEASE \
  https://git.savannah.nongnu.org/git/lwip.git /tmp/lwip
mkdir -p third_party/lwip/src third_party/lwip/include
# Copy the minimal set:
cp -r /tmp/lwip/src/core        third_party/lwip/src/
cp -r /tmp/lwip/src/netif       third_party/lwip/src/
cp -r /tmp/lwip/src/include/lwip third_party/lwip/include/
cp -r /tmp/lwip/src/include/netif third_party/lwip/include/
```

We can strip: `apps/`, `ipv6/`, PPP, DNS, SNMP, everything for our
milestone. Leave `arch/` empty — no OS.

### 11.2 `lwipopts.h` (drop under `third_party/lwip/include/`)

Trim to the minimum:

```c
#define NO_SYS                    1
#define LWIP_SOCKET               0
#define LWIP_NETCONN              0
#define LWIP_RAW                  0
#define LWIP_TCP                  1
#define LWIP_UDP                  0
#define LWIP_DHCP                 0
#define LWIP_ICMP                 1
#define LWIP_ARP                  1
#define MEM_ALIGNMENT             8
#define MEM_SIZE                  (256 * 1024)   /* generous — 16 GB avail */
#define PBUF_POOL_SIZE            32
#define TCP_MSS                   1460
#define TCP_SND_BUF               (16 * TCP_MSS)
#define TCP_WND                   (16 * TCP_MSS)
#define LWIP_STATS                1
#define SYS_LIGHTWEIGHT_PROT      0
```

### 11.3 `sys_arch.h` and `sys_arch.c`

With `NO_SYS=1` we need only:

- `sys_now()` — return milliseconds since boot. Read a system counter
  (`CNTVCT_EL0`) and divide. Trivial.
- Nothing else (no mutexes, no threads).

### 11.4 `netif` glue

`src/net.c` implements `netif_init`, `netif_linkoutput`
(→ `nic_tx`), and a poll function `netif_input` that pulls one frame
from `nic_rx` and hands it to `ethernet_input`.

Reference `AsahiLinux` has no lwIP; this is fully our code. Standard
lwIP pattern; ~120 lines.

### 11.5 Milestone

**M7:** From Comet Lake, `ping 192.168.0.246` returns responses.
`ethernet_input` → `etharp_input` → `icmp_input` → echo reply happens
inside lwIP; we just need to keep calling `netif_input` in the poll
loop.

---

<a id="12-phase-8--tcp"></a>
## §12 Phase 8 — TCP hello world server on port 3333

### 12.1 Application code — `src/hello.c`

Using lwIP's raw API:

```c
#include "lwip/tcp.h"

static const char *HELLO = "Hello World\n";

static err_t on_sent(void *arg, struct tcp_pcb *pcb, u16_t len)
{
    tcp_close(pcb);
    return ERR_OK;
}

static err_t on_accept(void *arg, struct tcp_pcb *newpcb, err_t err)
{
    if (err != ERR_OK) return err;
    tcp_sent(newpcb, on_sent);
    /* write may return ERR_MEM if TCP send buffer full — safe under our TCP_SND_BUF */
    tcp_write(newpcb, HELLO, strlen(HELLO), TCP_WRITE_FLAG_COPY);
    tcp_output(newpcb);
    return ERR_OK;
}

void hello_server_start(void)
{
    struct tcp_pcb *pcb = tcp_new();
    tcp_bind(pcb, IP_ANY_TYPE, 3333);
    struct tcp_pcb *listener = tcp_listen(pcb);
    tcp_accept(listener, on_accept);
}
```

### 12.2 Buffer lifecycle (the user's explicit requirement)

The lwIP TCP path already implements per-connection buffers:

- On SYN: `tcp_new_listen_pcb` is called internally; the accepted PCB
  is a new one for that connection. `tcp_alloc()` grabs it from
  `MEMP_TCP_PCB` (fixed pool, configured via `MEMP_NUM_TCP_PCB`).
- `pbuf`s for TX are allocated via `pbuf_alloc(PBUF_RAM, ...)`; for RX
  they come from `PBUF_POOL`. Both pools are static (§11.2).
- On FIN/RST from either side, the PCB is freed and returned to the pool.

The requirement is satisfied by lwIP's own architecture — we do not
have to write bespoke allocators. What we do have to check:

1. **Set `MEMP_NUM_TCP_PCB = 8`** (in `lwipopts.h`) — plenty for one
   `nc` at a time; small enough to be visibly bounded.
2. **Confirm no leaks under a stress run** — after `for i in {1..100}; do nc M4 3333; done`
   the running kernel's `lwip_stats.memp[MEMP_TCP_PCB].used` counter
   must return to 0. We'll print stats from `kmain` at exit for
   inspection.

### 12.3 Kernel main loop — final form

```c
int kmain(u64 nic_mmio, u64 dma_iova, void *ba)
{
    u8 mac[6] = MY_MAC;
    u8 ip[4]  = MY_IPV4;
    nic_init(nic_mmio, dma_iova, mac);
    lwip_init();
    netif_setup(mac, ip);
    hello_server_start();
    /* Bounded loop; caller re-enters until user Ctrl-C's the Python driver */
    for (int i = 0; i < 100000; i++) {
        netif_input_one_frame();   /* pump lwIP */
        sys_check_timeouts();
        /* Poll takes ~1us when idle; ~10-50us per received frame */
    }
    dc_puts("[kernel] lwip stats:\n");
    /* dump memp stats via dc_puts */
    return 0;
}
```

### 12.4 Python driver — `scripts/run_hello.py`

```python
#!/usr/bin/env python3
import sys, pathlib, time
sys.path.append("/home/ahmed/Projects/AsahiLinux/m4/m1n1/proxyclient")
from m1n1.setup import *
from m1n1.fw.smc import SMCClient
from m1n1.hw.dart import DART

# 1. Bring PCIe up
smc = SMCClient(u, u.adt["arm-io/smc"].get_reg(0)[0], None)
smc.start(); smc.start_ep(0x20)
smc.smcep.write32("gP0d", 0x800001)
smc.smcep.write32("gP1a", 1)
smc.stop()
assert p.pcie_init() == 0

# 2. Find NIC BAR0 (fill BDF from Phase 3 output)
BDF_DEV = 0  # from pcie-config-space.txt
ECAM = u.adt["/arm-io/apcie-ge0"].get_reg(0)[0]
cfg = ECAM + (BDF_DEV << 15)
bar0 = p.read32(cfg + 0x10) & ~0xf
p.write32(cfg + 0x04, p.read32(cfg + 0x04) | 0x6)   # enable

# 3. DART for NIC DMA
dart = DART.from_adt(u, "arm-io/dart-apcie-ge0"); dart.initialize()
BUF_SIZE = 256 * 1024
phys = p.memalign(0x4000, BUF_SIZE)
iova = dart.iomap_at(0, 0x40000, phys, BUF_SIZE)

# 4. Upload kernel
kernel = pathlib.Path("build/kernel.bin").read_bytes()
kaddr = p.memalign(0x4000, len(kernel))
iface.writemem(kaddr, kernel)
p.dc_cvau(kaddr, len(kernel))
p.ic_ivau(kaddr, len(kernel))

# 5. Enter server loop
print("Entering TCP hello-world loop. Ctrl-C to stop.")
try:
    while True:
        rc = p.call(kaddr, bar0, iova, 0)
        # rc is number of frames processed in that iteration; useful for tuning
except KeyboardInterrupt:
    print("Stopping.")
```

### 12.5 Milestones

**M8a:** `nc 192.168.0.246 3333` from Comet Lake returns
`Hello World\n` and closes. Reproducible.

**M8b:** After `for i in {1..100}; do nc 192.168.0.246 3333; done` the
kernel's `MEMP_TCP_PCB` used-count returns to 0.

**M8c (nice-to-have):** while the server is running, `ping M4_IP` still
works (ICMP path from Phase 7 is not broken by TCP path).

---

<a id="13-debugging-strategy"></a>
## §13 Debugging strategy

### 13.1 Layered failure modes

When the system stops responding, work down this list in order:

1. **Nothing on `/dev/m1n1-raw`.** m1n1 crashed or wasn't chainloaded.
   Power-cycle. If USB doesn't re-enumerate, hard-reset (long power hold).
2. **m1n1 boot log truncates.** Something in m1n1's own init hit a fault.
   Extremely unlikely from our work; check `dmesg` for USB disconnect
   timestamps.
3. **`p.call()` returns immediately with 0 and nothing printed.** Our
   entry stub is wrong. Double-check `_kentry` symbol is at file offset 0
   (`aarch64-linux-gnu-objdump -d build/kernel.elf | head -20`).
4. **`p.call()` hangs.** We're in a tight loop with no exit. Ctrl-C
   the Python client — `iface.nop()` will time out, then the shell
   recovers. On the next power cycle you get fresh state.
5. **DART fault printed by m1n1 after our call.** The NIC tried to DMA
   outside our IOVA range. Dump DART, verify the descriptor addresses
   we're programming into the NIC use `iova`, not `phys`.
6. **PCIe read returns `0xffffffff`.** The NIC lost its config-space
   registers (link went down, or bus master flag cleared). Re-enable
   MEM|BM in config +0x04.

### 13.2 Prints, prints, prints

Dockchannel TX is essentially free (a few `str`s to MMIO). Use it
liberally. Every state transition in the RX/TX path should emit a
one-line trace. Comment them out only when the milestone lands.

### 13.3 Bisect over builds

Each phase's milestone artifact stays reachable:

```
build/kernel-M2.bin   # hello world only
build/kernel-M3.bin   # after PCIe reach
build/kernel-M5.bin   # after broadcast frame
build/kernel-M6.bin   # after ARP
build/kernel-M7.bin   # after lwIP ping
build/kernel-M8.bin   # after TCP
```

Rename target on milestone. When a later change breaks something, we
can regress to the last known-good in ~5 seconds.

### 13.4 tcpdump on Comet Lake, always

Keep `sudo tcpdump -i eth0 -e -n host 192.168.0.246 -w /tmp/m4-live.pcap`
running in a terminal for the whole session. If lwIP misbehaves, the
pcap is the source of truth for what the M4 actually put on the wire.

### 13.5 Reset script

Have `scripts/reset.py` that does:

```python
try: p.reboot()
except: pass
```

...so a broken kernel state can be recovered with one command instead
of a physical power hold.

---

<a id="14-risks"></a>
## §14 Risks and pivot points

### R1: m1n1's `pcie_init()` doesn't recognize `apcie,t8132`

**Likelihood:** ~50 %. `m4n1` fork adds t8122/t6030/t6031 but not t8132.
**Cost:** 1–3 days to add compat + verify. Not a project-killer.
**Mitigation:** §7B path is spelled out. Pattern is straightforward.

### R2: The NIC is Apple's own SoC-integrated IP with no docs

**Likelihood:** ~30 %. Apple has been rolling more networking on-die.
Mac mini (Mac16,10) may still use a discrete PCIe NIC given the physical
RJ45 port, but this is unverified.
**Cost:** If yes, RE from scratch is 1–3 months.
**Mitigation:** Pivot to **USB-CDC-Ethernet** as network transport:
- Chainload out of m1n1 entirely (accept losing the USB console).
- Bring up the DWC3 USB controller ourselves (m1n1's `src/usb_dwc3.c`
  is a decent reference; MIT-licensed).
- Expose an ECM/NCM gadget.
- Comet Lake sees `usb1` netif; runs lwIP on our side.
Cost of pivot: ~3–4 weeks. Not preferred but a real fallback.

### R3: DART configuration for M4 differs from prior SoCs

**Likelihood:** low; DART tends to be very stable across SoCs.
**Cost:** hours to a day. Debug via `dart.dump_all()`.

### R4: lwIP integration blocks on some obscure config

**Likelihood:** low. `NO_SYS=1` is well-trodden ground.
**Cost:** hours. Don't panic; the lwIP wiki has a bare-metal walkthrough.

### R5: `p.call()` under `--call` mode leaves state that breaks m1n1 on return

**Likelihood:** low but real. `p.mmu_shutdown()` before call, then we
run without MMU. If our code accidentally trips a cache line into a
state m1n1 doesn't expect, subsequent proxy commands might fault.
**Cost:** re-chainload m1n1 after each call — iteration slows to ~10 s
per cycle instead of ~2 s. Painful but not blocking.
**Mitigation:** After every call, do `p.get_bootargs()` from Python.
If it hangs, immediate power cycle and re-enroll.

### R6: M4 mini's 192.168.0.246 IP is stale

**Likelihood:** medium. `192.168.0.246` was the macOS-assigned IP; our
bare-metal kernel has no persistent lease. If the DHCP server retained
that address for the macOS MAC, our static-config choice will conflict.
**Cost:** minutes. Change `MY_IPV4` to something outside the DHCP pool
(e.g. `192.168.0.99`), rebuild.
**Mitigation:** encoded in §10.1 already.

### R7: We picked the wrong PCIe controller (ge0 vs ge1)

**Likelihood:** medium. Both may exist; only one has the NIC.
**Cost:** minutes. Loop over all controllers in `pcie_up.py` and take
the one that yields a class-0x02 device.

---

<a id="15-first-session-commands"></a>
## §15 First-session concrete commands

Ordered. Run top-to-bottom the next time you sit down.

```bash
# 0. Sanity: are we in a git repo, in the right project?
cd /home/ahmed/Projects/C/embedded/AppleSiliconM4
git status

# 1. Environment
sudo pacman -S --needed python-construct python-pyserial picocom
sudo tee /etc/udev/rules.d/70-m1n1.rules <<'RULE'
SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="316d", ATTRS{bInterfaceNumber}=="00", MODE="0660", GROUP="uucp", SYMLINK+="m1n1"
SUBSYSTEM=="tty", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="316d", ATTRS{bInterfaceNumber}=="02", MODE="0660", GROUP="uucp", SYMLINK+="m1n1-raw"
RULE
sudo udevadm control --reload && sudo udevadm trigger
groups | grep -q uucp || { sudo usermod -aG uucp ahmed; echo "log out+in, or run: newgrp uucp"; }
mkdir -p /tmp/m4-recon scripts third_party include src

# 2. Power-cycle M4, wait for symlinks
ls -l /dev/m1n1 /dev/m1n1-raw

# 3. Boot log capture (background)
(picocom -b 115200 --logfile /tmp/m4-recon/m1n1-boot.log /dev/m1n1-raw &)
# Or: timeout 5 cat /dev/m1n1-raw > /tmp/m4-recon/m1n1-boot.log

# 4. Interactive m1n1 shell — do §5.3 through §5.8
cd /home/ahmed/Projects/AsahiLinux/m4/m1n1/proxyclient
./tools/shell.py
# Paste in the recon snippets from §5.3-§5.8

# 5. Read /tmp/m4-recon/recon-summary.md (write it after the shell exits)
$EDITOR /tmp/m4-recon/recon-summary.md
```

Once §5 is done and `recon-summary.md` has the four answers (PCIe
compat, NIC vendor:device + BAR0, MAC, DART node), proceed to §6.

That's it. Everything you need to know to reach M8 is in this document.
Read it top to bottom the first time; skim §3 and §13 whenever you get
stuck later.
