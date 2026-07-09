# Plan to complete PLAN_2.md

## Iteration model (unchanged from M2)

Every phase reuses the existing loop: edit C → `make` → `./Scripts/m1n1/*.sh` → read the `log_buf` back. The kernel side accumulates functionality; the Python side accumulates setup (SMC → PCIe → DART → BAR0 → kernel upload) before each `p.call()`.

`kmain`'s signature grows: currently `(log_buf, log_size, ba)`. Final form `(log_buf, log_size, nic_mmio, dart_iova)`. Zeros for args a given phase doesn't use yet.

Two harnesses will coexist:
- `smoke_test.sh` — stays as-is, unit-testing kernel logic without hardware setup.
- `run_hello.sh` (new, grows phase by phase) — does the full setup dance before each call.

---

## Phase 1 — Reconnaissance (Session 1)

**Deliverables**
- `Scripts/m1n1/recon.py` — three phases gated by CLI flags:
  - default: pure ADT read (dumps ADT, `/arm-io` children, PCIe `compatible` strings, MAC from `/chosen`, list of `dart-*` nodes)
  - `--pcie`: SMC power + `p.pcie_init()` + ECAM sweep across each `apcie*` base → identifies class-0x02 device, dumps its 256-byte config header
  - `--dart-dump`: `DART.from_adt(u, node).dump_all()` for each dart-apcie*
- `Scripts/m1n1/recon.sh` — wrapper matching `smoke_test.sh` conventions (sudo, `M1N1DEVICE=/dev/ttyACM0`).

**You run**
```bash
./Scripts/m1n1/recon.sh --pcie --dart-dump
```
Then paste `/tmp/m4-recon/recon-summary.md` back.

**M1:** four answers on disk (PCIe compat, NIC VID:DID+BAR0, MAC, DART node).

**Branches decided:**
- PCIe compat → §7A vs §7B (m1n1 patch).
- NIC VID:DID → which chip driver template (Broadcom `tg3` / Realtek `r8169` / Aquantia / worst-case Apple SoC IP).
- DART node → argument to Phase 4.

---

## Phase 3 — PCIe reach (Session 2)

**§7A (compat supported):**
- `Scripts/m1n1/pcie_up.py` — SMC power (`gP0d=0x800001`, `gP1a=1`), `p.pcie_init()`, ECAM walk to class-0x02 device, enable MEM+BM in CMD (config +0x04). Writes `/tmp/m4-recon/nic-runtime.txt` with BDF, BAR0, VID:DID.
- **M3:** `p.read32(bar0)` returns a plausible non-`0xffffffff` value.

**§7B (must patch m1n1):**
- Clone the `apcie,t8122` clause in `AsahiLinux/m4/m1n1/src/pcie.c:258-286` for `apcie,t8132`.
- Rebuild + re-enroll m1n1 (you run `bputil`/`kmutil` per your Notes/3.md).
- I'll flag this branch explicitly before touching `AsahiLinux/m4/`.

---

## Phase 4 — DART for NIC (Session 3)

**Deliverables**
- `Scripts/m1n1/dart_up.py` — `DART.from_adt(u, DART_NODE)`, `.initialize()`, `p.memalign(0x4000, 0x40000)` for a 256 KiB DMA buffer, `dart.iomap_at(0, 0x40000, phys, 0x40000)`. Writes `/tmp/m4-recon/nic-dma.txt`.
- No kernel changes.

**M4:** `dart.dump_all()` confirms L1/L2 entry at `iova` → `phys`.

---

## Phase 5 — NIC driver (Sessions 4-N, chip-dependent)

**LOC budget:** 400-800.

**Chip-specific abstract API**, chip-specific implementation:
- `include/nic.h` — `nic_init`, `nic_rx`, `nic_tx`, `nic_link_status` (chip-agnostic).
- `src/nic_<chip>.c` — chip-specific.
- Extend `kmain` signature; extend `upload_and_call.py` to pass `nic_mmio` and `dart_iova`.

**Kernel-side incremental milestones (each an M5-x sub-step, one smoke_test round-trip each):**
- M5a: read chip ID / revision register; compare to datasheet expected value.
- M5b: reset the controller; read a status bit that flips post-reset.
- M5c: program MAC filter, read it back.
- M5d: check PHY link status → prints "link up 1 Gbps FDX".
- M5e: TX one 64-byte broadcast frame; host tcpdump sees it.

**M5:** M5e passes.

**Fallback branch (R2):** if the NIC is Apple SoC-integrated with no docs, pivot to USB-CDC-ECM per §14.R2. I'll ask before committing to that; it changes the entire downstream plan (adds DWC3 bring-up, drops m1n1 console).

---

## Phase 6 — ARP (Session N+1)

**Deliverables**
- `include/net.h` — `MY_MAC` (from recon), `MY_IPV4` (default `192.168.0.246`; will switch to `192.168.0.99` if `ip neigh show` on host reveals the macOS-M4 lease is still active).
- `include/arp.h`, `src/arp.c` — handle EtherType 0x0806 opcode 1 (reply) and opcode 2 (cache).
- Kernel loop bounded to ~10 ms per `p.call()`; Python calls in a `while` loop.

**M6:** `arping -c 3 -I eth0 <IP>` from host → 3 replies; `ip neigh` shows the M4's MAC.

---

## Phase 7 — lwIP integration (Session N+2)

**Vendored code** — `third_party/lwip/` from savannah `STABLE-2_2_0_RELEASE`. Copy only `src/core/*.c`, `src/core/ipv4/*.c`, `src/netif/ethernet.c`, `src/include/{lwip,netif}/*`. Strip apps/, ipv6/, SNMP, PPP, DNS.

**New glue**
- `include/lwipopts.h` — `NO_SYS=1`, TCP+ARP+ICMP only, no UDP/DHCP, MEM_SIZE 256K, MEMP_NUM_TCP_PCB 8, TCP_MSS 1460.
- `src/sys_arch.c` — `sys_now()` from `CNTVCT_EL0` / `CNTFRQ_EL0`.
- `src/net.c` — `netif_init`, `linkoutput` → `nic_tx`, poll pulling from `nic_rx` into `ethernet_input`.

**Makefile** — add `-Wno-unused-parameter -Wno-sign-compare` scoped to lwIP sources.

**M7:** `ping -c 3 192.168.0.246` from host → 3 responses (lwIP's built-in ICMP handles it; validates the netif).

---

## Phase 8 — TCP hello world (Session N+3)

**Deliverables**
- `src/hello.c` — 30 lines, lwIP raw API: `tcp_new` → `tcp_bind(3333)` → `tcp_listen` → `tcp_accept` callback that `tcp_write("Hello World\n")` + `tcp_output`, then `tcp_close` in the `on_sent` callback.
- `Scripts/m1n1/run_hello.py` / `run_hello.sh` — full setup (SMC, `pcie_init`, DART, upload) then `while True: p.call(...)`.

**M8a:** `nc 192.168.0.246 3333` → `Hello World\n`.  
**M8b:** 100-round nc loop; `MEMP_TCP_PCB.used` back to 0 (printed via log buf).  
**M8c:** `ping` still works while server is up.

---

## Cross-cutting

- **File layout:** all new Python stays under `Scripts/m1n1/` (matches existing convention, not PLAN_2 §4.5's lowercase proposal).
- **AsahiLinux path:** all new scripts use the same `sys.path.append(~/Projects/AsahiLinux/m4/m1n1/proxyclient)` as `upload_and_call.py`.
- **You always run privileged commands.** I'll print exact commands including `sudo`, wait for output.
- **PLAN_2.md not modified.** It's the spec. If facts drift (e.g. recon shows a different DART node), I update the affected code and note the drift in commit messages.

