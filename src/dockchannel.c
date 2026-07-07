/*
 * dockchannel.c -- Polled TX for the T8132 dockchannel UART.
 *
 * Bytes written here surface on the Comet Lake host at
 * /dev/m1n1-raw (the second CDC-ACM interface of m1n1's USB gadget).
 * m1n1 already programmed the hardware; we just poll TX_FREE and
 * push bytes into TX8.
 */

#include "dockchannel.h"
#include "kernel.h"

static void dc_putbyte(u8 c)
{
    while (mmio_read32(DOCKCHANNEL_BASE_T8132 + DOCKCHANNEL_TX_FREE) == 0)
        ;
    mmio_write32(DOCKCHANNEL_BASE_T8132 + DOCKCHANNEL_TX8, c);
}

void dc_putc(char c)
{
    if (c == '\n')
        dc_putbyte('\r');
    dc_putbyte((u8)c);
}

void dc_puts(const char *s)
{
    while (*s)
        dc_putc(*s++);
}

static char hex_nibble(u8 n)
{
    n &= 0xf;
    return (n < 10) ? (char)('0' + n) : (char)('a' + n - 10);
}

void dc_puthex8(u8 v)
{
    dc_putc(hex_nibble(v >> 4));
    dc_putc(hex_nibble(v));
}

void dc_puthex32(u32 v)
{
    dc_putc('0');
    dc_putc('x');
    for (int shift = 28; shift >= 0; shift -= 4)
        dc_putc(hex_nibble((u8)(v >> shift)));
}

void dc_puthex64(u64 v)
{
    dc_putc('0');
    dc_putc('x');
    for (int shift = 60; shift >= 0; shift -= 4)
        dc_putc(hex_nibble((u8)(v >> shift)));
}

void dc_putdec(u64 v)
{
    char buf[21];
    int i = 0;

    if (v == 0) {
        dc_putc('0');
        return;
    }
    while (v) {
        buf[i++] = (char)('0' + v % 10);
        v /= 10;
    }
    while (i--)
        dc_putc(buf[i]);
}
