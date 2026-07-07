/*
 * start.S -- EL2 entry point for the T8132 bare-metal kernel.
 *
 * Contract with the loader (m1n1 chainload -r, or iBoot via fuOS):
 *   - We enter at EL2 with the MMU off.
 *   - x0 holds a pointer to the xnu-style boot_args struct.
 *   - All other GPRs are undefined; caches/interrupts are in an
 *     unspecified state (assume caches on, IRQs masked from vbar).
 *
 * We stay single-core for now: the primary CPU (MPIDR aff0==0)
 * continues into C; every other CPU parks in a WFE loop so that a
 * later SMP bring-up phase can wake them.
 */

    .section .text.boot, "ax"
    .global  _start
    .align   2

_start:
    /* Preserve boot_args pointer in a callee-saved register so it
     * survives every bl below without extra stack juggling. */
    mov     x19, x0

    /* Park secondary cores. MPIDR_EL1[7:0] holds aff0; on M-series
     * cores the primary boot CPU is aff0==0. */
    mrs     x1, mpidr_el1
    and     x1, x1, #0xff
    cbnz    x1, park

    /* Install exception vectors before anything can fault. Vector
     * base must be 2 KiB aligned; linker script guarantees that. */
    adrp    x2, _vectors
    add     x2, x2, :lo12:_vectors
    msr     vbar_el2, x2
    isb

    /* Set up the stack (grows down from _stack_top). */
    adrp    x2, _stack_top
    add     x2, x2, :lo12:_stack_top
    mov     sp, x2

    /* Zero the .bss so C static data starts clean. */
    adrp    x3, __bss_start
    add     x3, x3, :lo12:__bss_start
    adrp    x4, __bss_end
    add     x4, x4, :lo12:__bss_end
1:  cmp     x3, x4
    b.hs    2f
    str     xzr, [x3], #8
    b       1b
2:

    /* Hand off to C. Pass boot_args pointer as x0. */
    mov     x0, x19
    bl      kmain

    /* kmain should never return; if it does, fall through to park. */

park:
    wfe
    b       park
