/*
 * linker.ld -- Payload layout for the T8132 p.call() kernel.
 *
 * chainload.py -c uploads the raw binary at an address chosen by
 * m1n1's heap allocator, then calls that address as an AAPCS
 * function. So _kentry must sit at file offset 0 -- we pin its own
 * dedicated section first, ahead of all other .text.
 *
 * PIC by construction (adrp / :lo12: everywhere); the VMA below is
 * a fiction, the image runs from whatever base p.malloc picked.
 */

ENTRY(_kentry)

SECTIONS
{
    . = 0x0;

    .text : ALIGN(16) {
        KEEP(*(.text._kentry))
        *(.text .text.*)
    }

    .rodata : ALIGN(16) {
        *(.rodata .rodata.*)
    }

    .data : ALIGN(16) {
        *(.data .data.*)
    }

    .bss (NOLOAD) : ALIGN(8) {
        __bss_start = .;
        *(.bss .bss.*)
        *(COMMON)
        . = ALIGN(8);
        __bss_end = .;
    }

    /DISCARD/ : {
        *(.comment)
        *(.note .note.*)
        *(.eh_frame*)
        *(.ARM.attributes)
    }
}
