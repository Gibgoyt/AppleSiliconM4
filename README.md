# Long Term Goals

by the way this is not currently bare metal, logic sits in EL2, as right now m1n1 boot loader needs fast iteration
will eventually cut down to EL0

because Ahmed loves his databases we will make this bare metal kernel into a database
TCP for SQL, only one thread, either queuing like epoll or cqe/sqe like uring
IO Uring style for dispatching I/O, we only need NVME to work, PCIE drivers I think on this mac mini
having a few threads for dedicated I/O if we want an approach like libmdbx B+Tree, maybe even BεTree
Look at this paper https://www3.cs.stonybrook.edu/~bender/newpub/2015-BenderFaJa-login-wods.pdf
and dedicated compute for write heavy workloads with a stripped-down RocksDB approach might be nice
read workloads must be single producer single consumer but must support multiple consumer for MVCC just as bustub does

yeeeee, if i needs add more i will addz more

# hello-t8132 — bare-metal "Hello World" kernel for the Apple M4

Minimal AArch64 kernel that boots at EL2 on an Apple M4 Mac mini
(Mac16,10 / T8132), initializes the on-board debug UART, and prints
a banner plus a dump of `boot_args`. No MMU, no PMM, no interrupts,
no networking — that is all future phases described in `PLAN.md`.

This is the first executable milestone of the plan: proof that our
own code can take control on the machine and speak back over the
serial line.

## What it does

On entry (from `chainload.py -r -E 0` under a live m1n1 session, or
eventually from `kmutil configure-boot` once we produce a Mach-O):

1. Parks every non-primary CPU in a `wfe` loop.
2. Installs a debug exception vector table at `VBAR_EL2`.
3. Sets up a 64 KiB stack and zeroes `.bss`.
4. Calls `kmain(boot_args*)`, which prints:

   ```
   ================================================
     Hello World from bare-metal T8132!
     hello-t8132 v0.1
   ================================================

   [cpu]
     CurrentEL  = EL2
     MPIDR_EL1  = 0x...

   [boot_args]
     ptr        = 0x...
     revision   = 0x...
     version    = 0x...
     virt_base  = 0x...
     phys_base  = 0x800000000
     mem_size   = 0x400000000 (16384 MiB)
     ...
   ```

5. Halts on `wfe`. Any exception before the halt prints the
   ESR/ELR/FAR/SPSR and a decoded EC name.

## Layout

```
AppleSiliconM4/
├── Makefile         top-level build (cross-gcc + ld + objcopy)
├── linker.ld        payload layout; _start at file offset 0
├── include/
│   ├── types.h      u8..u64, NULL, size_t
│   ├── boot_args.h  xnu-style boot_args (mirrors m1n1 xnuboot.h)
│   ├── uart.h       Samsung S3C UART API
│   └── kernel.h     kmain, panic, hang, sysreg accessors, mmio
└── src/
    ├── start.S      EL2 entry: park secondaries, set VBAR, stack,
    │                zero BSS, call kmain
    ├── vectors.S    16-entry EL2 vector table (all fatal for now)
    ├── uart.c       polling UART driver at 0x3ad200000
    ├── panic.c      exception dumper + panic()
    └── main.c       kmain: banner + boot_args dump + halt
```

## Build

```
make
```

Outputs:

```
build/kernel.elf   linked ELF with debug info
build/kernel.bin   flat binary for chainload.py -r
build/kernel.dump  disassembly
```

Requires the `aarch64-linux-gnu-*` cross-toolchain (Arch package
`aarch64-linux-gnu-gcc`).

## Deploy

Assumes Phase A is complete — that is, m1n1 is enrolled as fuOS on
the M4 mini, the USB-C proxy cable is connected to the Comet Lake
dev host, and `proxyclient/tools/shell.py` responds. See
`PLAN.md §3` for how to reach that state.

From the Comet Lake host, with the M4 booted into m1n1:

```
cd /home/ahmed/Projects/AsahiLinux/m4/m1n1/proxyclient
./tools/chainload.py -r -E 0 \
    /home/ahmed/Projects/C/embedded/AppleSiliconM4/build/kernel.bin
```

The banner should appear on the same serial port (`/dev/ttyACM0`)
within a second.

If the raw entry point 0 causes trouble, the offset is a
command-line flag: `_start` is always the first byte of
`kernel.bin`, so `-E 0` is correct by construction of the linker
script.

## What The Fuck We Don't Want

- No MMU, so all memory access is Device-nGnRE or unmarked
  (whatever m1n1 left behind). Fine for polled UART.
  The **POESES** at apple make it so fucking hard to probe LPDDR, or maybe the problem is elsewhere
  also, fuck EL0! we can do everything at El1, EL0 only for bootloader m1n1, which does hardware bringup
- No IRQs. AIC is not touched. `vec_*_irq` handlers treat every
  interrupt as fatal.
- No `printf`. Hex/decimal helpers only; a formatter is easy to
  add later.
- No SMP. Secondaries are parked, never released.
- No Mach-O output. The raw binary path is enough to run under
  chainload; `kmutil configure-boot` (Phase A of the plan) needs a
  Mach-O wrapper we haven't written yet.

