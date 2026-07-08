/*
 * main.c -- kmain entry from _kentry (start.S).
 *
 * M2 behavior: initialize the log buffer supplied by Python, print
 * a banner + call args + CPU state into it, and return. Python
 * reads the buffer back after p.call() returns.
 *
 * Args from p.call(addr, x0, x1, x2, x3):
 *   x0 = log_buf   -- byte buffer for our text output
 *   x1 = log_size  -- capacity of that buffer
 *   x2 = ba        -- reserved for later phases (boot_args pointer)
 */

#include "kernel.h"
#include "log.h"

static void banner(void)
{
    log_puts("================================================\n");
    log_puts("  " KERNEL_NAME " v" KERNEL_VERSION " (p.call payload)\n");
    log_puts("================================================\n");
}

static void dump_call_args(u64 log_buf, u64 log_size, u64 ba)
{
    log_puts("[kernel] hello from bare-metal M4 payload\n");
    log_puts("[kernel] log_buf  = "); log_puthex64(log_buf);  log_putc('\n');
    log_puts("[kernel] log_size = "); log_puthex64(log_size); log_putc('\n');
    log_puts("[kernel] ba       = "); log_puthex64(ba);       log_putc('\n');
}

static void dump_cpu_state(void)
{
    log_puts("[kernel] CurrentEL = EL"); log_putdec(read_currentel()); log_putc('\n');
    log_puts("[kernel] MPIDR_EL1 = ");   log_puthex64(read_mpidr());   log_putc('\n');
}

int kmain(u64 log_buf, u64 log_size, u64 ba)
{
    log_init(log_buf, log_size);
    banner();
    dump_call_args(log_buf, log_size, ba);
    dump_cpu_state();
    log_puts("[kernel] returning to m1n1 proxy\n");
    return 0;
}
