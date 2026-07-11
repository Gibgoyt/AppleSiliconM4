---
name: ref-m4-repos
description: Repo paths + recon/log file locations for the Apple M4 mini PCIe bring-up project
metadata:
  type: reference
---

**Working repo (Python bring-up scripts):** `/home/ahmed/Projects/C/embedded/AppleSiliconM4`
- Main script: `Scripts/m1n1/perstn.py` (Phase 0..F pipeline)
- Wrapper: `Scripts/m1n1/perstn.sh` (sudo + env + runs perstn.py against `/dev/ttyACM0`)
- ADT / geometry helpers: `Scripts/m1n1/pcie_regs.py`
- Recon dumps: `m4_recon/` (nic-adt.txt etc.)

**m1n1 fork (C tree):** `~/Projects/AsahiLinux/m4/m1n1`
- pcie.c is at `src/pcie.c`. The t8132 clause pins `state->pcie_regs = &regs_t8140` (search "apcie,t8132").
- Proxy Python bindings: `proxyclient/m1n1/proxy.py`. `p.tunables_apply_local_addr` is BROKEN there (uses wrong constant) -- use `p.tunables_apply_local(path, prop, reg_idx)` and rely on num_phys==1 identity.

**Runtime log (from perstn.sh):** `/tmp/m4-recon/nic-runtime.txt`. Gets truncated + rewritten on every flush. Every Phase F step now flushes pre+post so the file always ends with the exact step name the wedge hit.

**How to apply:** When digging into m1n1 semantics, read the C source in the fork directly rather than guessing from the recon. When iterating on the bring-up, follow the [[project-m4-pcie-bringup]] state notes for what's already been ruled out.
