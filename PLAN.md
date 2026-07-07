# Bare-metal Kernel on M4 Mac mini with Built-in Ethernet — Execution Plan

> **Target:** boot a bare-metal AArch64 kernel on an M4 Mac mini (Mac16,10 / T8132) at EL2, bring up the on-board Ethernet controller through Apple's PCIe root complex + DART IOMMU, and broadcast `Hello World` as a UDP packet on the LAN.
>
> **Chosen path: B3 — built-in Ethernet, no USB dongle, no QEMU.**
>
> **Realistic time budget:** 6-18 months of evening/weekend work for one developer. This is not a weekend project. The plan below is broken into gated phases so you can pause between them without losing momentum.

---

## 0. Ground truth (already established, do not re-derive)

From the recon done on 2026-02-19 (see `/home/ahmed/Projects/AsahiLinux/m4/Notes/`):

| Fact | Value |
|---|---|
| Machine | M4 Mac mini, Mac16,10, T8132, 10 cores, 16 GB LPDDR |
| macOS | 15.1 Sequoia (24B2083) |
| ECID | `0x48A41E03001C` |
| Volume Group UUID | `9C404CDA-E560-4594-A99E-BEBA9587182C` |
| Debug UART base | `0x3ad200000` (Samsung S3C-style, ADT: `/arm-io/uart6`) |
| RAM base | `0x8_0000_0000` |
| Boot vector | `bputil -n -c -a -s -v <VUID>` + `kmutil configure-boot -c <macho> -v /Volumes/Macintosh\ HD` |
| iBoot entry state | EL2, MMU off, x0 = `boot_args*`, single-core active |
| Comet Lake dev host | `192.168.0.251`, cross-toolchain `aarch64-linux-gnu-*` installed |
| M4 mini LAN address | `192.168.0.246` (in recoveryOS; unknown after bare-metal takeover) |
| Pre-built m1n1 | `/home/ahmed/Projects/AsahiLinux/m4n1/build/m1n1.macho` (864 KB, `TARGET=T8132 BRINGUP`, no SMP) |
| Pre-written probe | `/home/ahmed/Projects/AsahiLinux/m4/baremetal/probe.{S,bin}` |

Everything below assumes this state. If any of it drifts (macOS updated, disk reflashed, IP changed), re-verify before continuing.

---

## 1. Reality check for B3

The other B options were rejected. Before we commit, understand exactly what B3 requires that B1/B2 did not:

**New drivers you must write from scratch, in this order:**

1. Apple **PMGR** clock/power gating for the PCIe controller (undocumented registers, cribbed from m1n1 and Linux `drivers/soc/apple/apple-pmgr-pwrstate.c`)
2. Apple **PCIe root complex** driver (not standard ECAM-only — Apple has custom init sequences, MSI programming, and link training quirks)
3. Apple **DART** (Device Address Resolution Table) IOMMU driver (must be programmed correctly or every DMA faults)
4. The NIC driver itself:
   - If **1 GbE** on M4 mini: Broadcom BCM57xx variant (chip TBD by recon)
   - If **10 GbE** upgrade: Marvell/Aquantia AQC113 (uses `atlantic` driver in Linux)
5. Ethernet framing + PHY link management
6. lwIP integration on top of all of the above

**What Asahi already figured out (that we will read, translate, but not directly reuse):**
- m1n1's PMGR/DART/PCIe helpers (BSD-licensed within m1n1, safe to reference)
- Linux `drivers/pci/controller/pcie-apple.c` (GPL — read to understand behavior, do not paste)
- Linux `drivers/iommu/apple-dart.c` (GPL — same)
- Linux `drivers/net/ethernet/broadcom/` or `drivers/net/ethernet/aquantia/atlantic/` (GPL — same)

**License hygiene rule:** we can read GPL for understanding, but code we ship must be original — no copy-paste. m1n1 is BSD/MIT-ish so we can lift small helpers with attribution. Datasheets (if we can find them) are the primary source of truth.

**Fallback ejection point:** if PCIe root complex bring-up stalls for more than ~6 weeks, cut losses and pivot to B2 (USB Ethernet dongle). Reserve the option in your head. Don't grind indefinitely.

---

## 2. Prerequisites and dev loop

### 2.1 Physical setup (verify all present)
- [ ] M4 Mac mini
- [ ] Comet Lake host (Linux, cross-toolchain, m1n1 checkouts already in place)
- [ ] USB-C data cable, M4 <-> Comet Lake (any USB 3.x-capable cable — used for m1n1's CDC serial proxy)
- [ ] Ethernet cable, M4 mini RJ45 -> LAN switch
- [ ] A second machine on the LAN running `nc -ul 8080` as the UDP receiver (any laptop / the Comet Lake box itself works)
- [ ] Machine Owner username + password for `bputil` (needed every time we re-enroll)

### 2.2 Directory layout

Kernel code goes in this repo (`/home/ahmed/Projects/C/embedded/AppleSiliconM4/`), NOT tangled with m1n1:

```
AppleSiliconM4/
├── PLAN.md                     <- this file
├── README.md
├── Makefile                    <- top-level build
├── linker.ld                   <- payload layout for iBoot / m1n1
├── include/
│   ├── kernel.h
│   ├── boot_args.h             <- struct boot_args (from m1n1 src/types.h)
│   ├── uart.h
│   ├── mmu.h
│   ├── pmm.h
│   ├── heap.h
│   ├── vectors.h
│   ├── aic.h
│   ├── timer.h
│   ├── pmgr.h
│   ├── dart.h
│   ├── pcie.h
│   ├── nic.h                   <- NIC HAL (whichever chip we find)
│   ├── ethernet.h
│   └── netif_glue.h            <- lwIP bridge
├── src/
│   ├── start.S                 <- EL2 entry, stack, .bss, jump to kmain
│   ├── vectors.S               <- exception table
│   ├── main.c                  <- kmain: init order + main loop
│   ├── panic.c                 <- ELR/ESR/FAR pretty-printer
│   ├── uart.c                  <- lift from probe.S, translate to C
│   ├── mmu.c
│   ├── pmm.c
│   ├── heap.c
│   ├── aic.c
│   ├── timer.c
│   ├── pmgr.c
│   ├── dart.c
│   ├── pcie.c
│   ├── nic_<vendor>.c          <- filled in during Phase F
│   ├── ethernet.c
│   ├── netif_glue.c
│   └── app.c                   <- UDP hello world
├── third_party/
│   └── lwip/                   <- submodule, unmodified upstream
├── tools/
│   ├── build_and_deploy.sh     <- rebuild + push over m1n1 proxy
│   └── udp_listener.py         <- host-side sanity checker
└── docs/
    ├── adt_dumps/              <- captured ADT snapshots per phase
    ├── pcie_regs/              <- annotated MMIO traces
    └── datasheets/             <- if you find any
```

### 2.3 Init toolchain check (before writing anything else)

```
aarch64-linux-gnu-gcc --version    # must be present
aarch64-linux-gnu-ld --version
aarch64-linux-gnu-objcopy --version
python3 -c "import serial, usb"
which dtc
```

Cross-compile a `hello.c` for `-ffreestanding -nostdlib -static` to a bare AArch64 ELF, disassemble, confirm it looks right. Do this before touching M4.

---

## 3. Phase A — Deploy m1n1, establish USB dev loop

**Purpose:** unblock every phase after this. Without m1n1 running, every code change means `bputil` + `kmutil` + reboot cycle, which is soul-crushing. With m1n1 running, every code change is `run_guest.py` + 5 seconds.

### 3.1 Rebuild m1n1 from `m4-bringup` branch (not mainline)

The pre-built `m4n1/build/m1n1.macho` came from mainline and lacks the T8132 SMP fixes. Rebuild from the bring-up branch:

```
cd /home/ahmed/Projects/AsahiLinux/m4/m1n1
git status                                       # confirm on m4-bringup
git pull                                         # get latest from yuyuyureka
# config.h: #define TARGET T8132 and #define BRINGUP
PATH="$HOME/.cargo/bin:$PATH" make -j$(nproc)    # cargo/rustc PATH quirk from Notes 3
ls -la build/m1n1.macho
```

### 3.2 Deploy per Notes 3

The full procedure is in `/home/ahmed/Projects/AsahiLinux/m4/Notes/3.md` — do not restate here, follow it verbatim. In summary: 1TR -> bash reverse shell -> `bputil -n -c -a -s -v <VUID>` -> `curl` binary -> `kmutil configure-boot -c /tmp/m1n1.macho -v /Volumes/Macintosh\ HD` -> connect USB-C to Comet Lake -> reboot.

### 3.3 Verify

On Comet Lake:
```
ls /dev/ttyACM*
cd /home/ahmed/Projects/AsahiLinux/m4/m1n1/proxyclient
./tools/shell.py /dev/ttyACM0
>>> hex(u.mrs(CURRENTEL))            # expect 0x8
>>> p.read32(0x3ad200000)            # should not fault
```

**Gate A:** m1n1 shell responds. `run_guest.py` can push and boot a Mach-O in <10 s. **Do not proceed until this works.** Every subsequent phase depends on it.

### 3.4 Fallback if m1n1 does not boot

- Try the pre-built mainline `m4n1/build/m1n1.macho` first — if that boots and rebuild does not, the m4-bringup branch regressed something.
- Try `probe.bin` via raw mode — it writes to the UART. If nothing at all comes out, either UART base is wrong or iBoot is not loading the binary.
- Log everything to `docs/phase_a_log.md` before moving on. Future you will thank you.

---

## 4. Phase B — Kernel foundations

**Purpose:** get to the point where we can safely call driver code that reads/writes MMIO and services interrupts.

### 4.1 `start.S` (EL2 entry)

```
_start:
    mrs   x1, mpidr_el1
    and   x1, x1, #0xff
    cbnz  x1, park_secondary          # single-core for now
    adrp  x2, _stack_top
    add   x2, x2, :lo12:_stack_top
    mov   sp, x2
    # zero .bss
    adrp  x3, __bss_start
    add   x3, x3, :lo12:__bss_start
    adrp  x4, __bss_end
    add   x4, x4, :lo12:__bss_end
1:  cmp   x3, x4
    b.hs  2f
    str   xzr, [x3], #8
    b     1b
2:  bl    kmain
    b     .
```

- Preserve `x0` (boot_args pointer) into `x19` before any bl.
- Install vectors before enabling interrupts.

### 4.2 Exception vectors (`vectors.S` + `panic.c`)

- 16-entry `VBAR_EL2` table, 2 KB aligned.
- Every handler saves x0-x30, calls a C `handle_exception(u64 esr, u64 elr, u64 far, u64 spsr, u64 vec)` that pretty-prints via UART.
- Decode `ESR_EL2` EC field: data abort, instruction abort, unknown, HVC, etc.
- On unhandled: dump 32 words of stack, spin.

**Rationale:** without this, MMIO faults are silent hangs. Absolutely required before touching PCIe.

### 4.3 UART driver (`uart.c`)

Port `probe.S`'s UART code to C. Register layout confirmed:
- `UTRSTAT` at `+0x10`, TX-empty is bit 1
- `UTXH` at `+0x20`
- `URXH` at `+0x24`

`uart_init()` does nothing (m1n1 already programmed it). Just poll and write.

### 4.4 MMU (`mmu.c`)

Decision: **VHE at EL2** (`HCR_EL2.E2H=1`, program EL1 aliases). Simpler than juggling EL2/EL1 splits. m1n1 also uses VHE — you can crib register setup patterns.

- 16 KB pages (Apple's native granule).
- 3-level table (48-bit VA).
- Identity map:
  - DRAM `[0x8_0000_0000, 0x8_0000_0000 + mem_size)` as normal, cacheable, inner+outer WB.
  - MMIO regions (`0x2xx_xxxx_xxxx`, `0x3xx_xxxx_xxxx`) as `Device-nGnRE`.
- `MAIR_EL1`, `TCR_EL1`, `TTBR0_EL1`, then `SCTLR_EL1.M=1`, `isb`.
- No user-space, no ASIDs.

**Gate B.1:** kernel prints `mmu on\n` after enabling.

### 4.5 Physical page allocator (`pmm.c`)

- Simple bitmap over the DRAM range m1n1 reports as usable in `boot_args`.
- 16 KB granularity.
- `pmm_alloc()` / `pmm_free()`, plus `pmm_alloc_contig(n_pages, align)` for DMA buffers.

### 4.6 Kernel heap (`heap.c`)

Bump allocator on top of `pmm_alloc`, upgraded to freelist buckets later. Don't premature-optimize.

### 4.7 AIC (Apple Interrupt Controller)

- M4 uses AIC v3.
- Reference: m1n1 `src/aic.c`.
- Enough functionality for now: mask/unmask a hwirq, ACK, register a C handler.
- MSI programming will get added during Phase E (PCIe needs it).

### 4.8 Timer

- `CNTVCT_EL0` + `CNTFRQ_EL0` for monotonic.
- `CNTV_CVAL_EL0` + `CNTV_CTL_EL0` for one-shot IRQ.
- `udelay()`, `mdelay()`, `now_ms()`, callback-based `timer_after_ms(ms, cb, arg)`.

### 4.9 PMGR (`pmgr.c`)

Before any peripheral driver can talk to hardware, its clock domain must be ungated. On Apple silicon this is PMGR.

- Read PMGR base from ADT (`/arm-io/pmgr`).
- Each device in ADT has a `clock-gates` property that lists the PMGR registers + bits to program.
- Copy m1n1's `pmgr_adt_power_enable(path)` helper API (translate to your style).

**Gate B:** kmain does the full init sequence up through `pmgr_init()` without faulting; heap smoke test (`kmalloc` a million times) passes; a deliberate NULL-deref triggers the exception handler and prints ESR/ELR/FAR.

---

## 5. Phase C — Reconnaissance (do this BEFORE writing PCIe code)

**Purpose:** you cannot write a driver for a chip you have not identified. This phase produces the answers that determine Phase F's shape.

### 5.1 Identify the NIC while still in macOS/recoveryOS

Before losing macOS to bare metal, capture from macOS:
```
ioreg -l -p IOService | grep -iE 'ethernet|bcm|aquantia|atlantic|network' -A 10 > docs/ioreg_ethernet.txt
system_profiler SPEthernetDataType > docs/ethernet_profile.txt
pmset -g                     # power management context
```

Record:
- Vendor/device IDs (VID:DID)
- ADT node path (e.g. `/arm-io/apcie/pci-bridge0/bcm5719`)
- MAC address (for later sanity check)
- Whether it's 1 GbE or 10 GbE

### 5.2 Dump the ADT from m1n1

From m1n1 shell (Phase A output):
```
from m1n1.adt import ADT
adt = u.adt
# Walk /arm-io looking for pcie / dart nodes
for node in adt.walk_tree():
    if 'pcie' in node.name or 'dart' in node.name or 'ether' in node.name:
        print(node.name, node.reg if hasattr(node,'reg') else '')
```

Save the raw ADT to `docs/adt_dumps/phase_c.bin` and a decoded YAML representation to `docs/adt_dumps/phase_c.yaml`. **Every subsequent driver needs these addresses.**

### 5.3 Determine what you're up against

Based on the recon, expect one of:

| NIC | Driver ref | Difficulty |
|---|---|---|
| Broadcom BCM57xx family (1 GbE) | Linux `tg3` or `bnxt` | Hard — Broadcom is closed, no public datasheet, tg3 is the closest thing to docs |
| Marvell/Aquantia AQC113 (10 GbE) | Linux `atlantic` | Very hard — AQC113 firmware handshake is complex, but atlantic is well-organized code |
| Something else entirely | ??? | Stop and reassess |

Write your findings into `docs/nic_identification.md`. **Gate C:** you know exactly which chip, its VID:DID, its ADT path, its PCIe bus/device/function, and which DART instance sits in front of it.

---

## 6. Phase D — DART (IOMMU)

**Purpose:** enable DMA. Without DART programmed, every PCIe device write to memory faults.

### 6.1 Understanding

DART is Apple's IOMMU. Each PCIe controller sits behind one DART instance. Registers include:
- `DART_PARAMS` (page size, table count)
- `DART_TCR` per stream (translation control)
- `DART_TTBR` per stream (translation base)
- `DART_TLB_OP` (invalidate)

Reference: m1n1 `src/dart.c`, Linux `drivers/iommu/apple-dart.c`.

### 6.2 What to build

- `dart_init(int instance)` — read ADT for base address + params, disable bypass, program empty page tables.
- `dart_map(instance, iova, phys, size, perms)` — install identity or explicit mappings for DMA buffers.
- `dart_unmap(instance, iova, size)`.
- `dart_alloc_iova(instance, size)` — simple bump allocator in a private IOVA range.
- TLB invalidate helpers with wait-for-idle.

### 6.3 Verify without PCIe

You can smoke-test DART by:
- Programming a mapping.
- Reading back the TTBR/TCR you wrote.
- Attempting a bypass and confirming a fault event (if the DART has one).

You can't fully test until PCIe DMA is running, but partial verification here saves debugging chaos later.

**Gate D:** DART instance for the Ethernet-side PCIe controller is initialized, mappings can be created/torn down, invalidations complete without hang.

---

## 7. Phase E — PCIe root complex

**Purpose:** enumerate the bus, find the Ethernet function, assign BARs, set up MSIs.

### 7.1 Apple PCIe controller specifics

Apple's PCIe is not stock DesignWare. Key oddities:
- Custom power-on sequence with PMGR + reset GPIOs (the reset GPIOs come from `smc` and/or `gpio` blocks — read the ADT `perst-gpios` property).
- Reference clocks driven by Apple clock gens; enable per-port refclk before link training.
- Config space access is memory-mapped (ECAM-style) but at a controller-specific base.
- Link training must be waited on (`LTSSM` state polled), typically ~50 ms per port.
- MSI programming uses AIC's MSI extension — study m1n1's AIC code for the MSI mailbox layout.

Reference: Linux `drivers/pci/controller/pcie-apple.c` — read carefully, do not copy.

### 7.2 What to build

- `pcie_init(controller_idx)`:
  1. `pmgr_adt_power_enable("/arm-io/apcieN")`
  2. Assert then release PERST via GPIO
  3. Program refclk, LTSSM, wait for link up
  4. Set up ECAM window
- `pcie_config_read/write(bus, dev, fn, off, width)` — plain ECAM after init.
- Enumeration: BFS from bus 0, allocate BARs from a fixed 32-bit and 64-bit MMIO pool per host bridge.
- MSI: allocate a vector from AIC's MSI range, program the device's MSI Cap block, hook the vector to a C handler.

### 7.3 Milestone chain (tick them off one at a time)

1. `pcie_init` returns; no fault.
2. Config-space read of bus 0 dev 0 returns a real VID:DID (Apple's host bridge).
3. Iterate bus 0 — find the downstream bridge that leads to Ethernet.
4. Config-space read of the NIC returns the VID:DID matched from Phase C.
5. Allocate BAR0, map through DART, read the NIC's first register — get a plausible value.
6. Attach an MSI vector, trigger a self-MSI (if the NIC supports it) or a doorbell that raises interrupts, see the AIC IRQ fire in your handler.

**Gate E:** you have the NIC's BAR0 mapped, can read/write its registers, and can receive an interrupt from it.

This is where 40% of Phase B-E's time will actually be spent. Expect weeks.

---

## 8. Phase F — NIC driver

**Purpose:** send and receive raw Ethernet frames.

### 8.1 Common structure regardless of chip

- `nic_probe()` — read chip ID from BAR0, sanity-check against expected VID:DID.
- `nic_reset()` — chip-specific reset sequence.
- `nic_read_mac(uint8_t out[6])` — most NICs put the MAC in an EEPROM/OTP; read via chip-specific register sequence.
- Ring buffers:
  - TX descriptor ring in DMA-mappable memory (`pmm_alloc_contig`, `dart_map` to IOVA).
  - RX descriptor ring, pre-populated with buffers ready to receive.
- `nic_start()` — enable RX, TX, link status IRQ.
- `nic_send(const void *frame, size_t len)` — build descriptor, ring doorbell.
- `nic_isr()` — service TX complete, RX complete, link change.
- `nic_poll()` — pump descriptor rings if we're not fully interrupt-driven.

### 8.2 Chip-specific paths

**If Broadcom (tg3 lineage):**
- No public datasheet — Linux `drivers/net/ethernet/broadcom/tg3.c` and `tg3.h` are the reference.
- Expect firmware-less operation (tg3 chips don't need a firmware blob).
- Bring-up order roughly: chip reset -> load config from NVRAM -> program MAC filters -> allocate rings -> enable engines.
- PHY sits behind MII; poll PHY status register 1 for link.
- Budget: **3-4 months** for a minimal one-way "send a broadcast frame" then another 1-2 months for RX and PHY link.

**If Aquantia AQC113 (10 GbE):**
- Linux `drivers/net/ethernet/aquantia/atlantic/` is the reference.
- Firmware handshake required — the AQ firmware runs on the NIC and you talk to it via a mailbox in MMIO. Get the mailbox right first.
- More descriptor-driven; MSI-X capable but you can use single MSI to start.
- Budget: **4-6 months** to a working TX+RX, with the firmware handshake being the biggest single blocker.

### 8.3 Milestone chain

1. Chip ID matches. `nic_probe()` returns success.
2. MAC address readback matches what macOS reported in Phase C.
3. PHY link status reports 1000 Mbps or 10 Gbps up (plug in the cable).
4. Craft a raw broadcast Ethernet frame (`dst=ff:ff:ff:ff:ff:ff, src=<our_mac>, ethertype=0x88b5` custom) in a DMA buffer. Ring the TX doorbell.
5. On the receiver (Comet Lake, `sudo tcpdump -i <if> -e ether host ff:ff:ff:ff:ff:ff`), see the frame.
6. Configure RX filter to accept broadcast + our unicast MAC. Post RX descriptors. Observe an incoming ARP/LLDP frame in your `nic_isr`, print its length.

**Gate F:** raw frame TX visible in Wireshark; raw frame RX printed on UART. This is the *single biggest milestone* of the entire project. Celebrate. Take a week off if you need it.

---

## 9. Phase G — Ethernet + ARP + IP + UDP via lwIP

**Purpose:** stop hand-crafting frames; let lwIP do the protocol lifting.

### 9.1 Add lwIP

```
cd third_party
git submodule add https://git.savannah.nongnu.org/git/lwip.git
git -C lwip checkout STABLE-2_2_0_RELEASE
```

Copy `lwip/contrib/examples/example_app/lwipopts.h` as a starting template; strip to `NO_SYS=1` and enable UDP + DHCP + ARP + IPv4. Disable TCP, PPP, IPv6 for now — you don't need them.

### 9.2 What you write

- `sys_now()` — returns `now_ms()` from the timer.
- `low_level_output(struct netif *n, struct pbuf *p)`:
  - Coalesce the pbuf chain into a single contiguous DMA buffer (or scatter-gather if the NIC supports it).
  - Call `nic_send()`.
- Poll loop in `main`:
  - `nic_poll()` -> for each received frame, allocate a `pbuf`, copy in, call `netif->input(pbuf, netif)`.
  - `sys_check_timeouts()`.

### 9.3 Bring up the netif with DHCP

```
struct netif nif;
netif_add(&nif, NULL, NULL, NULL, NULL, low_level_init, ethernet_input);
netif_set_default(&nif);
netif_set_link_up(&nif);       // after PHY link event
netif_set_up(&nif);
dhcp_start(&nif);
```

Poll `nif.ip_addr` until non-zero, print it to UART.

**Gate G:** you can `ping <m4_ip>` from any LAN machine and see replies. lwIP handles ARP + ICMP automatically.

---

## 10. Phase H — UDP Hello World

**Purpose:** ship the thing you set out to build.

```c
static void app_broadcast_hello(void) {
    struct udp_pcb *pcb = udp_new();
    udp_bind(pcb, IP_ADDR_ANY, 0);
    ip_addr_t bcast;
    IP4_ADDR(&bcast, 255, 255, 255, 255);
    ip_set_option(pcb, SOF_BROADCAST);

    struct pbuf *p = pbuf_alloc(PBUF_TRANSPORT, 12, PBUF_RAM);
    memcpy(p->payload, "Hello World\n", 12);
    udp_sendto(pcb, p, &bcast, 8080);
    pbuf_free(p);
}
```

Call once per second from the main loop (via a timer callback).

On the receiver LAN machine:
```
nc -ul 8080
```

**Gate H (the finish line):** every second, "Hello World" appears on the receiver. You wrote a bare-metal kernel with a PCIe + DART + NIC driver stack from scratch on an undocumented Apple SoC. This is a real accomplishment.

---

## 11. Debugging strategy

- **UART is your only interactive sense organ.** Log verbosely; add compile-time log levels so you can strip later.
- **m1n1 stays alive under you.** Even if your kernel jumps to a bad address and hangs, m1n1's proxy is still there — you can hit Ctrl-C in `run_guest.py`, get back to the m1n1 shell, and poke MMIO from Python to diagnose. Do not overwrite m1n1's memory region.
- **Every driver init function must print "X init: entering" and "X init: OK" so a hang in early bring-up is bisectable.**
- **Wireshark on the LAN switch's mirror port** (or a Linux box with `tcpdump -i any -w cap.pcap`) is the ground truth for network debugging.
- **Do NOT trust ADT strings — trust ADT addresses.** Node names occasionally shift between macOS versions; MMIO base addresses are more stable.
- **Snapshot working state to git often.** After every gate passes, tag the commit (`git tag phase-e-msi-works`). This project has enough failure modes that you'll want checkpoints.

---

## 12. References (bookmark these)

- **m1n1 source:** `/home/ahmed/Projects/AsahiLinux/m4/m1n1/` (bring-up branch) — primary reference for AIC, PMGR, DART, PCIe, USB, boot flow.
- **Apple ADT structure:** m1n1 `src/adt.c` + `proxyclient/m1n1/adt.py`.
- **AArch64 Architecture Reference Manual (ARM DDI 0487)** — MMU, exception model, MSR/MRS encodings.
- **Linux PCIe Apple driver:** `drivers/pci/controller/pcie-apple.c` (read for understanding).
- **Linux DART driver:** `drivers/iommu/apple-dart.c`.
- **Linux NIC driver (chip TBD):** to be determined at Gate C.
- **lwIP docs:** `savannah.nongnu.org/projects/lwip/` — read the "raw API" and "porting" chapters.
- **Notes from prior session:** `/home/ahmed/Projects/AsahiLinux/m4/Notes/{1,2,3}.md`.

---

## 13. Risks and pivot points

| Risk | Trigger | Response |
|---|---|---|
| M4 PCIe controller has undocumented quirks we can't reverse | Stuck at LTSSM state training after 3-4 weeks | Ask on Asahi Matrix `#asahi-dev`; consider swapping to B2 |
| DART reveals an Apple-specific stream ID mapping we can't guess | Every DMA fault flag lit despite correct mapping | Ask on Asahi Matrix; instrument m1n1 to trace macOS's DART programming |
| NIC needs a signed firmware blob only macOS knows how to load | NIC responds to reset but never gets to link | Extract firmware from macOS install (there's precedent for this — Asahi Wi-Fi does it) |
| Time budget blows past 12 months | Milestones in Phase E-F not landing on schedule | Downgrade goal to "UDP hello via B1 (USB-CDC proxy)" and revisit B3 later |
| M4 boot policy shifts under a macOS update | `bputil`/`kmutil` behavior changes | Re-verify Phase A before touching anything else |

---

## 14. What to do next session (concrete first steps)

1. Verify Phase A prerequisites still hold: cross-toolchain, m1n1 checkouts, Comet Lake network.
2. Rebuild m1n1 from `m4-bringup` branch (§3.1).
3. Follow Notes 3 deployment procedure to enroll `m1n1.macho` as fuOS (§3.2).
4. Connect USB-C, reboot, get `/dev/ttyACM0`, run `proxyclient/tools/shell.py` (§3.3).
5. Capture ADT dump for `docs/adt_dumps/phase_a.bin` — you'll refer to it for months.
6. Only after Gate A: scaffold this repo's `src/` and start Phase B.

Do not skip ahead. Every phase gate is there because the phases after it become 10x harder without it.
