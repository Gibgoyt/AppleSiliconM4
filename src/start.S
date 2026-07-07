/*
 * start.S -- Entry stub for the T8132 p.call() payload.
 *
 * Contract with m1n1 (call mode, chainload.py -c):
 *   - m1n1 has already flushed caches and shut down its MMU
 *     (mmu_shutdown), but it stays resident in memory.
 *   - VBAR_EL2 still points at m1n1's exception handlers; we do not
 *     touch it.
 *   - We are invoked as a plain AAPCS function via P_CALL (opcode 2,
 *     see AsahiLinux/m4/m1n1/src/proxy.h). Args in x0-x3, return in x0.
 *   - When we RET, m1n1 resumes uartproxy_run() and USB stays alive.
 *
 * The linker script pins .text._kentry at file offset 0 so
 * upload_and_call.py can pass the raw upload address to p.call().
 */

    .section .text._kentry, "ax"
    .global  _kentry
    .type    _kentry, %function
    .align   4
_kentry:
    stp     x29, x30, [sp, #-16]!
    mov     x29, sp
    bl      kmain
    ldp     x29, x30, [sp], #16
    ret
    .size _kentry, . - _kentry
