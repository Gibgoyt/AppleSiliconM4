---
name: reference-m4-existing-work
description: Where Ahmed's prior M4 bare-metal work lives — notes, m1n1 checkouts, pre-built binaries, custom probe source
metadata:
  type: reference
---

For [[project-m4-baremetal]], substantial prior work exists outside the current working directory. **Read these before proposing anything new** so you don't duplicate or contradict.

**Notes (recon + procedure, ~600 lines total):**
- `/home/ahmed/Projects/AsahiLinux/m4/Notes/1.md` — system identity, security state, disk layout
- `/home/ahmed/Projects/AsahiLinux/m4/Notes/2.md` — `bputil`/`kmutil` procedure, boot flow theory
- `/home/ahmed/Projects/AsahiLinux/m4/Notes/3.md` — end-to-end deploy procedure, UART details, open questions

**m1n1 checkouts (two of them, they differ):**
- `/home/ahmed/Projects/AsahiLinux/m4/m1n1/` — on branch `m4-bringup` from `yuyuyureka` fork, has T8132 SMP fixes. **This is the preferred base for rebuilds.**
- `/home/ahmed/Projects/AsahiLinux/m4n1/` — on `main` branch, T8122 (M3) focused. The pre-built `m1n1.macho` in `build/` came from here with `TARGET=T8132 BRINGUP` config override, so it lacks the SMP fixes.

**Pre-built binaries (from 2026-02-19, not yet deployed):**
- `/home/ahmed/Projects/AsahiLinux/m4n1/build/m1n1.macho` (864 KB, TARGET=T8132, BRINGUP mode, no SMP)
- `/home/ahmed/Projects/AsahiLinux/m4/baremetal/probe.bin` (824 bytes, custom raw AArch64)
- `/home/ahmed/Projects/AsahiLinux/m4/baremetal/probe.S` (300 lines, source for probe.bin) — good template for early bare-metal AArch64 asm on M4

**Build quirk to remember:** m1n1 build on Ahmed's Arch Linux needs `PATH="$HOME/.cargo/bin:$PATH"` prepended so rustup's rustc wins over the system rustc.
