# Plan to complete PLAN_2.md

## Iteration model (unchanged from M2)

Every phase reuses the existing loop: edit C → `make` → `./Scripts/m1n1/*.sh` → read the `log_buf` back. The kernel side accumulates functionality; the Python side accumulates setup (SMC → PCIe → DART → BAR0 → kernel upload) before each `p.call()`.

`kmain`'s signature grows: currently `(log_buf, log_size, ba)`. Final form `(log_buf, log_size, nic_mmio, dart_iova)`. Zeros for args a given phase doesn't use yet.

Two harnesses will coexist:
- `smoke_test.sh` — stays as-is, unit-testing kernel logic without hardware setup.
- `run_hello.sh` (new, grows phase by phase) — does the full setup dance before each call.

---

## Phase 1 — Reconnaissance ✅ COMPLETED

Achieved across commits:
- `847bf7c` — first recon.py landed (had a dangerous `--pcie` sweep,
  see below)
- `10e12eb` — first partial `m4_recon/` after crash (Phase A only)
- `d77d458` — recon.py cleaned up: safe `--pcie`, NIC ADT walker,
  split safe/gated DART dumps, richer summary. Full `m4_recon/`
  refreshed.

**Cold-start pointer for future sessions.** Everything Phase 1
discovered is on disk under `m4_recon/`. To pick up this project
without conversation memory, read in this order:

    m4_recon/recon-summary.md  <- start here; distilled findings
    m4_recon/nic-adt.txt       <- NIC + DART link resolution
    m4_recon/pcie-nodes.txt    <- PCIe compat strings + reg tables
    m4_recon/mac-address.txt   <- MAC from ADT /chosen
    m4_recon/darts.txt         <- every dart-* node inventory
    m4_recon/adt.txt           <- full ADT text dump (~964 KB) to
                                  cross-check anything

Then read `Scripts/m1n1/recon.py` at HEAD to understand what
`--pcie` and `--dart-dump` do today vs. what was originally planned.
The docstring and the `phase_b`/`phase_c` comments carry the "why"
behind the gating.

If any of the above files are missing (fresh clone, wiped repo),
regenerate them by running (m1n1 must be up on `/dev/ttyACM0` first):

    ./Scripts/m1n1/recon.sh          # safe, pure ADT, ~5 s
    cp /tmp/m4-recon/* m4_recon/     # promote into the repo

### What --pcie currently does (and does NOT do)

Today `recon.sh --pcie` runs only SMC power (`gP0d=0x800001`,
`gP1a=1`) + `p.pcie_init()`. **It no longer sweeps ECAM.** On the
first attempt (before `d77d458`) the sweep tried `p.read32` on
`/arm-io/apciec0` reg[0] = `0x1c50000000` while the fabric was
unrouted; that returns SLVERR → CPU sync abort → m1n1 dead → USB
gadget gone → power-cycle required. Same failure mode applies to
constructing `DART.from_adt(u, "arm-io/dart-apcie2")` (reads
`0x492000000`), which is why `--dart-dump` now gates all
`dart-apcie*` / `dart-apciec*` targets behind `--pcie` and wraps
each attempt in try/except.

**Discovery of NIC PCI VID:DID + BAR0, and dart-apcie2 L1/L2 dumps,
are moved to Phase 3 / Phase 4.** They cannot happen safely until
m1n1 is patched with the `apcie,t8132` clause.

### Rule for any future session

apcie/apciec MMIO is off-limits until Phase 3 lands. Concretely,
until m1n1 knows `apcie,t8132`, do NOT:

- read any address in `0x1c50000000..0x1cbfffffff` (per-slot ECAMs
  + main apcie MMIO)
- read any DART MMIO under `dart-apcie*` or `dart-apciec*` (bases
  around `0x49[0-f]000000`)
- call `DART.from_adt(u, "arm-io/dart-apcie*")` or `-apciec*`

Any of those trips a fabric SLVERR that kills m1n1 mid-transaction
and burns a full power-cycle.

---

## Phase 3 — PCIe reach (Session 2)

**Determined by Phase 1: we are on the §7B branch** (m1n1 needs
patching before `pcie_init` can bring up the M4's PCIe).

**Backport strategy: wholesale replace.** The sibling fork
`~/Projects/AsahiLinux/m4n1/` diverges from `m4/m1n1` in 30
source files. Rather than cherry-pick the T8122 slice out of
`pcie.c` and hope we caught every quirk, we lift the entire files
`pcie.c` + `pmgr.c` verbatim from `m4n1`, then add just the
`apcie,t8132` clause. Rationale:

- `m4n1`'s tree is a known-boots superset — copying whole files
  removes the chance of a missed quirk gate.
- The T6031 code paths that come along are inert on T8132 (the
  compat string never matches).
- `pmgr.c` **must** come along: T8132's ADT uses the new
  `ps-groups` format handled by `pmgr_use_group_and_offset`,
  which `pcie_init_controller` reaches through
  `pmgr_adt_power_enable`. Without the pmgr backport, PCIe
  bring-up fails even with a correct `pcie.c`.

### 3.1 File-level backports

1. **`src/pcie.c`** — copy `~/Projects/AsahiLinux/m4n1/src/pcie.c`
   over `~/Projects/AsahiLinux/m4/m1n1/src/pcie.c` verbatim.
2. **`src/pmgr.c`** — copy `~/Projects/AsahiLinux/m4n1/src/pmgr.c`
   over `~/Projects/AsahiLinux/m4/m1n1/src/pmgr.c` verbatim.
3. **Add `apcie,t8132` clause** to the freshly-copied pcie.c,
   after the `apcie,t8122` else-if:

   ```c
   } else if (adt_is_compatible(adt, adt_offset, "apcie,t8132")) {
       fuse_bits = NULL;
       state->pcie_regs = &regs_t8122;
       printf("pcie: Initializing t8132 PCIe controller\n");
   ```

   Phase 1 recon proved the fit: M4's `/arm-io/apcie` has 25 reg
   entries and `#ports = 3`, matching `regs_t8122`'s
   `shared_reg_count = 7` + 3×6.

**Dependency check (verified 2026-07-09):**
- `pcie.h`, `pmgr.h` — identical between the two trees.
- `adt.h` — m4n1 only *adds* `ADT_FOREACH_PROPERTY` + helpers
  (additive); pcie.c/pmgr.c don't use them.
- `utils.h` — one struct field renamed (`cyc_ovrd` →
  `apple_sysregs_unlocked`); pcie.c/pmgr.c don't reference it.

**Out of scope for this patch:** `apciec,t8132` (per-slot
Thunderbolt RCs). The NIC lives on `apcie`, not `apciec`.

### 3.2 Rebuild + re-enroll

```bash
cd ~/Projects/AsahiLinux/m4/m1n1
cp build/m1n1.macho build/m1n1.macho.prepatch     # rollback binary
PATH="$HOME/.cargo/bin:$PATH" make -j$(nproc)     # -> build/m1n1.macho
cp build/m1n1.macho /tmp/m4-serve/m1n1.macho      # for HTTP fetch
```

Ahmed executes the M4-side enrollment per Notes/3.md:200-221:
`curl` the macho, `kmutil configure-boot -c /tmp/m1n1.macho -v
/Volumes/Macintosh\ HD`, reboot.

Success signal on the next boot log:

    TTY> pcie: Initializing t8132 PCIe controller
    (instead of "Unsupported compatible")

### 3.3 pcie_up.py — actual PCIe bring-up

- `Scripts/m1n1/pcie_up.py` — SMC power (same three writes as
  `recon.py --pcie`), `p.pcie_init()`, then a **full ECAM walk**
  of `/arm-io/apcie` (reg[0] = `0x1cb0000000`) covering BDF
  `00:00.0` and each of the three `pci-bridge{0,1,2}` downstream
  buses. Logs every non-`0xffffffff` VID:DID and class code.
- For the class-0x02 device on `pci-bridge2`'s downstream bus:
  enable MEM+BM (config+0x04 |= 0x6), read BAR0.
- Every ECAM read wrapped in try/except so a partial link-up
  leaves `/tmp/m4-recon/nic-runtime.txt` behind instead of a
  silent SLVERR reboot.

**M3:** `p.read32(bar0)` returns a plausible non-`0xffffffff`
value. `nic-runtime.txt` committed to `m4_recon/` closes out
`m4_recon/recon-summary.md` Q2/Q3.

### 3.4 Rollback path

If the patched m1n1 breaks boot:

- Long-press power off → 1TR.
- `curl -o /tmp/m1n1.macho.prepatch http://192.168.0.251:8080/…`
  and `kmutil configure-boot -c /tmp/m1n1.macho.prepatch -v
  /Volumes/Macintosh\ HD`.
- Reboot into the previous known-good m1n1.

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
- **apcie/apciec MMIO is off-limits until Phase 3.** Reading `0x1c[5-b]0000000` (ECAMs) or `0x49[0-f]000000` (DARTs) without a completed `pcie_init` triggers a fabric SLVERR that kills m1n1. Full list in Phase 1 § "Rule for any future session".

