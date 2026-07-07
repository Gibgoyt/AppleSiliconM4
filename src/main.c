/*
 * main.c -- kmain entry from start.S.
 *
 * Responsibilities in this bring-up-only build:
 *   1. Bring the UART up in software (m1n1 already programmed the
 *      hardware; we just latch the base address).
 *   2. Print a banner so a human on the serial line can tell that
 *      *our* kernel got control (as opposed to m1n1 hanging, or
 *      iBoot silently faulting).
 *   3. Dump a few fields from boot_args as a sanity check that the
 *      loader's calling convention is what we assumed.
 *   4. Spin. There is no scheduler, no MMU, no IRQs -- everything
 *      after this is future phases in PLAN.md.
 */

#include "kernel.h"
#include "uart.h"
#include "boot_args.h"

static void banner(void)
{
    uart_puts("\n\n");
    uart_puts("================================================\n");
    uart_puts("  Hello World from bare-metal T8132!\n");
    uart_puts("  " KERNEL_NAME " v" KERNEL_VERSION "\n");
    uart_puts("================================================\n");
}

static void dump_boot_args(struct boot_args *ba)
{
    uart_puts("\n[boot_args]\n");
    uart_puts("  ptr        = ");
    uart_put_hex64((u64)ba);
    uart_puts("\n  revision   = ");
    uart_put_hex32(ba->revision);
    uart_puts("\n  version    = ");
    uart_put_hex32(ba->version);
    uart_puts("\n  virt_base  = ");
    uart_put_hex64(ba->virt_base);
    uart_puts("\n  phys_base  = ");
    uart_put_hex64(ba->phys_base);
    uart_puts("\n  mem_size   = ");
    uart_put_hex64(ba->mem_size);
    uart_puts(" (");
    uart_put_dec(ba->mem_size >> 20);
    uart_puts(" MiB)\n  top_of_kd  = ");
    uart_put_hex64(ba->top_of_kernel_data);
    uart_puts("\n  devtree    = ");
    uart_put_hex64((u64)ba->devtree);
    uart_puts("\n  dt_size    = ");
    uart_put_hex32(ba->devtree_size);
    uart_puts("\n  machine    = ");
    uart_put_hex32(ba->machine_type);
    uart_puts("\n");
}

static void dump_cpu_state(void)
{
    uart_puts("\n[cpu]\n  CurrentEL  = EL");
    uart_put_dec(read_currentel());
    uart_puts("\n  MPIDR_EL1  = ");
    uart_put_hex64(read_mpidr());
    uart_puts("\n");
}

void kmain(struct boot_args *ba)
{
    /* The UART hardware is already configured by m1n1/iBoot; we
     * only need to remember its base. */
    uart_init(UART_BASE_T8132);

    banner();
    dump_cpu_state();

    if (ba)
        dump_boot_args(ba);
    else
        uart_puts("\n[boot_args] pointer was NULL -- skipping dump\n");

    uart_puts("\nkmain: done. Halting on WFE.\n");
    hang();
}
