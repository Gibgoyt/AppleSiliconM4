/*
 * panic.c -- Kernel panic message + halt.
 *
 * m1n1 owns VBAR_EL2 in p.call() mode, so real faults go through
 * m1n1's exception dumper. This file is just for C-level asserts.
 * The message is appended to the log buffer if one was supplied.
 */

#include "kernel.h"
#include "log.h"

__attribute__((noreturn))
void panic(const char *msg)
{
    log_puts("\n*** KERNEL PANIC ***\n  ");
    log_puts(msg ? msg : "(no message)");
    log_puts("\nSystem halted.\n");
    hang();
    __builtin_unreachable();
}
