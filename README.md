# m4-payload — bare-metal AArch64 kernel for Apple M4 (Mac16,10 / T8132)

Minimal payload that runs under a live m1n1 session on an M4 Mac mini
via `chainload.py -c` (call mode). It prints a banner + the three
`p.call()` arguments through the dockchannel UART and returns; m1n1
resumes its uartproxy loop, and iteration continues without a power
cycle. See `PLAN_2.md` for the road map to a TCP hello-world server
on the LAN.

## Model

`p.call()` payload. m1n1 stays resident:

- Uploaded and invoked over USB-CDC-ACM (`/dev/m1n1`).
- m1n1 has already flushed caches and shut its own MMU down before
  the call; we run identity-mapped, physical.
- `_kentry` is a plain AAPCS leaf function pinned at file offset 0.
  Args land in `x0-x2`; return in `x0`.
- No MMU, no VBAR install, no BSS zero, no SMP release. m1n1 owns
  all of that.

## Layout

```
AppleSiliconM4/
├── Makefile            top-level build + `make deploy`
├── linker.ld           _kentry pinned at file offset 0
├── PLAN_2.md           live plan (M2..M8)
├── PLAN.md             historical, do not modify
├── README.md           this file
├── include/
│   ├── types.h
│   ├── kernel.h        kmain, panic, hang, sysreg + mmio helpers
│   ├── boot_args.h     xnu-style boot_args (unused at M2; kept for later)
│   └── dockchannel.h   dockchannel UART API
└── src/
    ├── start.S         _kentry leaf (stp/bl kmain/ldp/ret)
    ├── panic.c         panic() over dockchannel
    ├── dockchannel.c   polled TX driver at 0x388128000
    └── main.c          kmain: banner + call args + return
```

## Build

```
make                    # -> build/kernel.{elf,bin,dump}
```

Requires the `aarch64-linux-gnu-*` cross toolchain.

## Deploy (versioned staging under /tmp/m4-serve)

```
make deploy
# -> /tmp/m4-serve/kernel-<git-sha>.bin
# -> /tmp/m4-serve/kernel-latest.bin -> kernel-<git-sha>.bin
```

`/tmp/m4-serve/` is a staging/archival location on the Comet Lake
host. It is *not* the runtime transport — see `PLAN_2.md §2.6`. The
Mac mini receives kernels over USB-CDC via `chainload.py -c`, not
HTTP.

## Run on the M4

With m1n1 running (LEDs blinking, `/dev/m1n1` present):

```
cd $M1N1/proxyclient
./tools/chainload.py -c $KERNEL/build/kernel.bin
```

Expected on `/dev/m1n1-raw` (dockchannel):

```
================================================
  m4-payload v0.2 (p.call payload)
================================================

[kernel] hello from bare-metal M4 payload
[kernel] nic_mmio = 0x0000000000000000
[kernel] dma_iova = 0x0000000000000000
[kernel] ba       = 0x0000000000000000
[kernel] CurrentEL = EL2
[kernel] MPIDR_EL1 = 0x...
[kernel] returning to m1n1 proxy
```

Iteration cycle after the first upload: edit C → `make` →
`chainload.py -c` → observe. ~2–5 s per cycle.

## Deliberately absent

Everything past M2 is future work in `PLAN_2.md`:

- PCIe reach (§7), DART for NIC DMA (§8)
- NIC driver (§9), ARP responder (§10)
- lwIP raw-API TCP stack (§11)
- Port-3333 hello-world server (§12)
