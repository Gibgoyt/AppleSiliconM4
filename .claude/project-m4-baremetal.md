---
name: project-m4-baremetal
description: Ahmed's ongoing project — bare-metal AArch64 kernel on an M4 Mac mini with a from-scratch driver stack ending in UDP-over-built-in-Ethernet
metadata:
  type: project
---

Ahmed is building a bare-metal AArch64 kernel on his own M4 Mac mini (Mac16,10 / T8132, 10 cores, 16 GB), with the end goal of broadcasting `Hello World` as a UDP packet on the LAN through the machine's built-in Ethernet.

**Why:** hobby / learning project. Wants a real, non-emulated result, not a QEMU or USB-dongle shortcut. Chose the hardest of three networking paths — see [[feedback-b3-hardest-path]].

**How to apply:**
- The full execution plan is at `/home/ahmed/Projects/C/embedded/AppleSiliconM4/PLAN.md`. Read it before proposing anything so you don't reinvent phases.
- Prior recon notes and pre-built binaries live in `/home/ahmed/Projects/AsahiLinux/m4/` — see [[reference-m4-existing-work]].
- Current working directory `/home/ahmed/Projects/C/embedded/AppleSiliconM4/` is where new kernel code goes; the AsahiLinux directories are read-only reference.
- Dev host is a Linux machine called "Comet Lake" at LAN IP `192.168.0.251`. The M4 mini's LAN IP in recoveryOS was `192.168.0.246`, but changes once bare-metal takes over.
- Boot vector is the `bputil` + `kmutil configure-boot` fuOS flow (not m1n1 debug USB-C cable). Volume Group UUID `9C404CDA-E560-4594-A99E-BEBA9587182C`, ECID `0x48A41E03001C`.
- Debug UART at `0x3ad200000` (Samsung S3C-style). RAM base `0x8_0000_0000`.
- As of last work session (2026-02-19) Ahmed had built `m1n1.macho` and a custom `probe.bin` but had not yet deployed either. Phase A of the plan is "actually deploy and verify m1n1 dev loop works."
- Project time budget is 6-18 months. Do not push for aggressive completion timelines.
