/*
 * panic.c -- Exception dumper + kernel panic.
 *
 * Anything unexpected -- data abort, undefined instruction, an
 * explicit panic() call from C -- lands here, prints what it can
 * to the UART, and spins. There is no unwinding: this is bring-up
 * code, and a fault at this stage means "read the ESR and think".
 */

#include "kernel.h"
#include "uart.h"

static const char * const vec_names[16] = {
    "curr_sp0_sync",   "curr_sp0_irq",   "curr_sp0_fiq",   "curr_sp0_serror",
    "curr_spx_sync",   "curr_spx_irq",   "curr_spx_fiq",   "curr_spx_serror",
    "low64_sync",      "low64_irq",      "low64_fiq",      "low64_serror",
    "low32_sync",      "low32_irq",      "low32_fiq",      "low32_serror",
};

static void decode_ec(u64 esr)
{
    /* ESR_ELx.EC lives in bits [31:26]. Only the common cases get
     * a name -- everything else prints the raw class. */
    u32 ec = (u32)((esr >> 26) & 0x3f);
    uart_puts("  EC=");
    uart_put_hex8((u8)ec);
    uart_puts(" (");
    switch (ec) {
    case 0x00: uart_puts("unknown"); break;
    case 0x0e: uart_puts("illegal_state"); break;
    case 0x15: uart_puts("svc64"); break;
    case 0x16: uart_puts("hvc64"); break;
    case 0x17: uart_puts("smc64"); break;
    case 0x18: uart_puts("msr_mrs_trap"); break;
    case 0x20: uart_puts("iabort_lower"); break;
    case 0x21: uart_puts("iabort_same"); break;
    case 0x22: uart_puts("pc_alignment"); break;
    case 0x24: uart_puts("dabort_lower"); break;
    case 0x25: uart_puts("dabort_same"); break;
    case 0x26: uart_puts("sp_alignment"); break;
    case 0x2f: uart_puts("serror"); break;
    case 0x30: uart_puts("bp_lower"); break;
    case 0x31: uart_puts("bp_same"); break;
    case 0x3c: uart_puts("brk"); break;
    default:   uart_puts("?");
    }
    uart_puts(")\n");
}

void exception_dump(u64 vec, u64 esr, u64 elr, u64 far, u64 spsr)
{
    uart_puts("\n\n!!! EXCEPTION !!!\n  vec=");
    uart_put_hex64(vec);
    if (vec < 16) {
        uart_puts(" (");
        uart_puts(vec_names[vec]);
        uart_puts(")");
    }
    uart_puts("\n  ESR_EL2 = ");
    uart_put_hex64(esr);
    uart_puts("\n  ELR_EL2 = ");
    uart_put_hex64(elr);
    uart_puts("\n  FAR_EL2 = ");
    uart_put_hex64(far);
    uart_puts("\n  SPSR_EL2= ");
    uart_put_hex64(spsr);
    uart_puts("\n");
    decode_ec(esr);
    uart_puts("System halted.\n");
}

__attribute__((noreturn))
void panic(const char *msg)
{
    uart_puts("\n\n*** KERNEL PANIC ***\n  ");
    uart_puts(msg ? msg : "(no message)");
    uart_puts("\nSystem halted.\n");
    hang();
    __builtin_unreachable();
}
