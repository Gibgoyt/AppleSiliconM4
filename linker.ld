/*
 * linker.ld -- Payload layout for the T8132 bare-metal kernel.
 *
 * We're loaded as a raw blob by m1n1's chainload.py -r, which drops
 * the image at whatever base its heap allocator picks and jumps to
 * (base + entry_point). We pass -E 0, so _start must live at
 * offset 0 of the raw binary.
 *
 * All addressing in our code is PC-relative (adrp/:lo12:, no
 * absolute pointers baked in), so the actual VMA below is a
 * fiction -- the image relocates itself just fine at any 4 KiB-
 * aligned load address.
 */

ENTRY(_start)

SECTIONS
{
    . = 0x0;

    /* .text.boot MUST come first: chainload.py -E 0 jumps to
     * the very first byte of the image. */
    .text : ALIGN(16) {
        KEEP(*(.text.boot))
        *(.text.vectors)
        *(.text .text.*)
    }

    .rodata : ALIGN(16) {
        *(.rodata .rodata.*)
    }

    .data : ALIGN(16) {
        *(.data .data.*)
    }

    /* BSS is zeroed by start.S using these symbols. Keep 8-byte
     * alignment so the "str xzr, [x3], #8" loop is safe. */
    .bss (NOLOAD) : ALIGN(8) {
        __bss_start = .;
        *(.bss .bss.*)
        *(COMMON)
        . = ALIGN(8);
        __bss_end = .;
    }

    /* 64 KiB kernel stack, grows down from _stack_top. */
    .stack (NOLOAD) : ALIGN(16) {
        _stack_bottom = .;
        . = . + 0x10000;
        _stack_top = .;
    }

    /DISCARD/ : {
        *(.comment)
        *(.note .note.*)
        *(.eh_frame*)
        *(.ARM.attributes)
    }
}
