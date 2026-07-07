/*
 * vectors.S -- EL2 exception vector table.
 *
 * AArch64 vector tables are 2 KiB, split into four groups of four
 * 128-byte slots:
 *
 *   +0x000  Current EL with SP_EL0   { sync, irq, fiq, serror }
 *   +0x200  Current EL with SP_ELx   { sync, irq, fiq, serror }  <-- us
 *   +0x400  Lower EL AArch64         { sync, irq, fiq, serror }
 *   +0x600  Lower EL AArch32         { sync, irq, fiq, serror }
 *
 * We treat every entry as fatal for now: dispatch to the common
 * dumper with the vector index and spin. That way any surprise fault
 * during bring-up prints its ESR/ELR/FAR instead of silently hanging.
 */

    .section .text.vectors, "ax"
    .align   11              /* 2 KiB alignment required by VBAR */
    .global  _vectors
_vectors:

    /* --- Current EL with SP_EL0 --- */
    .align 7
    b       vec_curr_sp0_sync
    .align 7
    b       vec_curr_sp0_irq
    .align 7
    b       vec_curr_sp0_fiq
    .align 7
    b       vec_curr_sp0_serror

    /* --- Current EL with SP_ELx (this is where our own faults land) --- */
    .align 7
    b       vec_curr_spx_sync
    .align 7
    b       vec_curr_spx_irq
    .align 7
    b       vec_curr_spx_fiq
    .align 7
    b       vec_curr_spx_serror

    /* --- Lower EL, AArch64 --- */
    .align 7
    b       vec_low64_sync
    .align 7
    b       vec_low64_irq
    .align 7
    b       vec_low64_fiq
    .align 7
    b       vec_low64_serror

    /* --- Lower EL, AArch32 --- */
    .align 7
    b       vec_low32_sync
    .align 7
    b       vec_low32_irq
    .align 7
    b       vec_low32_fiq
    .align 7
    b       vec_low32_serror

    /*
     * Trampolines: pack the vector index into x0 and jump to the
     * common dumper. We do NOT try to be reentrant or return from
     * the exception -- this is a debug-only, hang-on-fault handler.
     */
vec_curr_sp0_sync:      mov x0, #0;  b common_dump
vec_curr_sp0_irq:       mov x0, #1;  b common_dump
vec_curr_sp0_fiq:       mov x0, #2;  b common_dump
vec_curr_sp0_serror:    mov x0, #3;  b common_dump
vec_curr_spx_sync:      mov x0, #4;  b common_dump
vec_curr_spx_irq:       mov x0, #5;  b common_dump
vec_curr_spx_fiq:       mov x0, #6;  b common_dump
vec_curr_spx_serror:    mov x0, #7;  b common_dump
vec_low64_sync:         mov x0, #8;  b common_dump
vec_low64_irq:          mov x0, #9;  b common_dump
vec_low64_fiq:          mov x0, #10; b common_dump
vec_low64_serror:       mov x0, #11; b common_dump
vec_low32_sync:         mov x0, #12; b common_dump
vec_low32_irq:          mov x0, #13; b common_dump
vec_low32_fiq:          mov x0, #14; b common_dump
vec_low32_serror:       mov x0, #15; b common_dump

    /*
     * common_dump(vec_id):
     *   Reads ESR/ELR/FAR/SPSR at EL2 into arg registers and calls
     *   exception_dump(vec, esr, elr, far, spsr). Never returns.
     *
     * We deliberately use SP as-is (whatever was current at the
     * exception) rather than switching stacks -- keeps this handler
     * short and the fault context recognizable in a debugger.
     */
common_dump:
    mrs     x1, esr_el2
    mrs     x2, elr_el2
    mrs     x3, far_el2
    mrs     x4, spsr_el2
    bl      exception_dump
1:  wfe
    b       1b
