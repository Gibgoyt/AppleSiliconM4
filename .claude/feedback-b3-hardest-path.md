---
name: feedback-b3-hardest-path
description: For this project Ahmed chose the hardest networking path (built-in Ethernet via PCIe+DART+NIC) after being warned about scope; don't keep re-pitching the safer alternatives
metadata:
  type: feedback
---

For the [[project-m4-baremetal]] project, Ahmed explicitly picked **B3 (write PCIe root complex + DART IOMMU + built-in Ethernet NIC driver from scratch)** over two safer alternatives that were laid out for him:
- B1: USB-CDC proxy shortcut (1 day to LAN-visible UDP)
- B2: USB Ethernet dongle with well-documented AX88179A (3-6 months)
- B3: Built-in Ethernet (6-12+ months, undocumented Apple silicon) — **chosen**

**Why:** Ahmed wants to actually USE the physical Ethernet port on his Mac. He rejected QEMU explicitly ("I do not want to do Qemu!"). He specifically wants a real hardware NIC driver, not a shortcut.

**How to apply:**
- Do not re-propose B1 (USB-CDC proxy) or B2 (USB dongle) as the *primary* path — that decision is settled.
- It's still fine to mention them as **pivot points if B3 stalls badly** (the plan has this at §13).
- When helping with tricky PCIe / DART / NIC bring-up, do not suggest "let's just use a dongle instead" — that's the whole thing he opted out of.
- Ahmed knows the scope. Don't caveat every response with reminders that this is hard — he's already committed.
