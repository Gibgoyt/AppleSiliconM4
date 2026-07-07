#ifndef KERNEL_UART_H
#define KERNEL_UART_H

#include "types.h"

#define UART_BASE_T8132  0x3ad200000ULL

void uart_init(u64 base);
void uart_putc(char c);
void uart_puts(const char *s);
void uart_put_hex64(u64 v);
void uart_put_hex32(u32 v);
void uart_put_hex8(u8 v);
void uart_put_dec(u64 v);

#endif
