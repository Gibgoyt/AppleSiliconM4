/*
 * panic.c -- Kernel panic message + halt.
 *
 * In call mode m1n1 owns VBAR_EL2, so any real fault vectors into
 * m1n1's exception dumper. This file is just for C-level asserts
 * that want a clean message + hang before returning to m1n1.
 */

#include "kernel.h"
#include "dockchannel.h"

__attribute__((noreturn))
void panic(const char *msg)
{
    dc_puts("\n\n*** KERNEL PANIC ***\n  ");
    dc_puts(msg ? msg : "(no message)");
    dc_puts("\nSystem halted.\n");
    hang();
    __builtin_unreachable();
}
