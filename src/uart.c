/*
 * uart.c -- Debug UART driver for T8132.
 *
 * The chip is a Samsung S3C-style UART; m1n1/iBoot leaves it
 * fully configured (baud, 8N1, TX enabled) before jumping to us,
 * so we only need to poll the TX-empty bit and stuff bytes into
 * the TX holding register. Registers:
 *
 *   +0x10  UTRSTAT   bit 1 = TX buffer empty
 *   +0x20  UTXH      write byte to transmit
 *   +0x24  URXH      read received byte (unused here)
 */

#include "uart.h"
#include "kernel.h"

#define UTRSTAT  0x010
#define UTXH     0x020

#define UTRSTAT_TX_EMPTY  (1u << 1)

static u64 uart_base;

void uart_init(u64 base)
{
    uart_base = base;
}

void uart_putc(char c)
{
    while ((mmio_read32(uart_base + UTRSTAT) & UTRSTAT_TX_EMPTY) == 0)
        ;
    mmio_write32(uart_base + UTXH, (u8)c);
}

void uart_puts(const char *s)
{
    while (*s) {
        if (*s == '\n')
            uart_putc('\r');
        uart_putc(*s++);
    }
}

static char hex_nibble(u8 n)
{
    n &= 0xf;
    return (n < 10) ? (char)('0' + n) : (char)('a' + n - 10);
}

void uart_put_hex8(u8 v)
{
    uart_putc(hex_nibble(v >> 4));
    uart_putc(hex_nibble(v));
}

void uart_put_hex32(u32 v)
{
    uart_putc('0');
    uart_putc('x');
    for (int shift = 28; shift >= 0; shift -= 4)
        uart_putc(hex_nibble((u8)(v >> shift)));
}

void uart_put_hex64(u64 v)
{
    uart_putc('0');
    uart_putc('x');
    for (int shift = 60; shift >= 0; shift -= 4)
        uart_putc(hex_nibble((u8)(v >> shift)));
}

void uart_put_dec(u64 v)
{
    char buf[21];
    int i = 0;

    if (v == 0) {
        uart_putc('0');
        return;
    }
    while (v) {
        buf[i++] = (char)('0' + v % 10);
        v /= 10;
    }
    while (i--)
        uart_putc(buf[i]);
}
