/*
 * main.c -- kmain entry from _kentry (start.S).
 *
 * PLAN_2 M2: print a banner + the three call args via the
 * dockchannel UART and return. m1n1 stays alive; the Python driver
 * on the Comet Lake host observes the return value and can re-call.
 *
 * Args come from p.call(addr, x0, x1, x2, x3):
 *   x0 = nic_mmio  (Phase 3 will populate; 0 for now)
 *   x1 = dma_iova  (Phase 4 will populate; 0 for now)
 *   x2 = ba        (boot_args pointer if we ever want it; 0 for now)
 */

#include "kernel.h"
#include "dockchannel.h"

static void banner(void)
{
    dc_puts("\n\n");
    dc_puts("================================================\n");
    dc_puts("  " KERNEL_NAME " v" KERNEL_VERSION " (p.call payload)\n");
    dc_puts("================================================\n");
}

static void dump_call_args(u64 nic_mmio, u64 dma_iova, u64 ba)
{
    dc_puts("\n[kernel] hello from bare-metal M4 payload\n");
    dc_puts("[kernel] nic_mmio = "); dc_puthex64(nic_mmio); dc_putc('\n');
    dc_puts("[kernel] dma_iova = "); dc_puthex64(dma_iova); dc_putc('\n');
    dc_puts("[kernel] ba       = "); dc_puthex64(ba); dc_putc('\n');
}

static void dump_cpu_state(void)
{
    dc_puts("[kernel] CurrentEL = EL"); dc_putdec(read_currentel()); dc_putc('\n');
    dc_puts("[kernel] MPIDR_EL1 = "); dc_puthex64(read_mpidr()); dc_putc('\n');
}

int kmain(u64 nic_mmio, u64 dma_iova, u64 ba)
{
    banner();
    dump_call_args(nic_mmio, dma_iova, ba);
    dump_cpu_state();
    dc_puts("[kernel] returning to m1n1 proxy\n");
    return 0;
}
