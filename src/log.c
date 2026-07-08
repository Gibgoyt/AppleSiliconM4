/*
 * log.c -- Bounded byte-buffer logger for the m1n1 p.call() payload.
 *
 * Python allocates a buffer via u.memalign(), passes its address +
 * size as kmain args, and reads the buffer back with iface.readmem()
 * after the call returns. See Scripts/m1n1/upload_and_call.py.
 */

#include "log.h"

static char *log_ptr;
static char *log_end;

void log_init(u64 buf, u64 size)
{
    log_ptr = (char *)buf;
    log_end = log_ptr + size;
    if (log_ptr && size > 0)
        *log_ptr = 0;
}

void log_putc(char c)
{
    /* Reserve one byte for the trailing null. */
    if (!log_ptr || log_ptr + 1 >= log_end)
        return;
    *log_ptr++ = c;
    *log_ptr = 0;
}

void log_puts(const char *s)
{
    while (*s)
        log_putc(*s++);
}

static char hex_nibble(u8 n)
{
    n &= 0xf;
    return (n < 10) ? (char)('0' + n) : (char)('a' + n - 10);
}

void log_puthex8(u8 v)
{
    log_putc(hex_nibble(v >> 4));
    log_putc(hex_nibble(v));
}

void log_puthex32(u32 v)
{
    log_putc('0');
    log_putc('x');
    for (int shift = 28; shift >= 0; shift -= 4)
        log_putc(hex_nibble((u8)(v >> shift)));
}

void log_puthex64(u64 v)
{
    log_putc('0');
    log_putc('x');
    for (int shift = 60; shift >= 0; shift -= 4)
        log_putc(hex_nibble((u8)(v >> shift)));
}

void log_putdec(u64 v)
{
    char buf[21];
    int i = 0;

    if (v == 0) {
        log_putc('0');
        return;
    }
    while (v) {
        buf[i++] = (char)('0' + v % 10);
        v /= 10;
    }
    while (i--)
        log_putc(buf[i]);
}
