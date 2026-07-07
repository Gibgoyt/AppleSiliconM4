#ifndef KERNEL_DOCKCHANNEL_H
#define KERNEL_DOCKCHANNEL_H

#include "types.h"

/*
 * Dockchannel UART on T8132. m1n1 has already programmed the hardware
 * and its base address (from ADT /arm-io/dockchannel-uart) resolves to
 * 0x388128000 on the current M4 boot logs. Register layout mirrors
 * AsahiLinux/m4/m1n1/src/dockchannel_uart.c:11-14.
 *
 * We only implement polled TX; RX is present if we ever want it.
 */

#define DOCKCHANNEL_BASE_T8132  0x388128000ULL

#define DOCKCHANNEL_TX8         0x4004
#define DOCKCHANNEL_TX_FREE     0x4014
#define DOCKCHANNEL_RX8         0x401c
#define DOCKCHANNEL_RX_COUNT    0x402c

void dc_putc(char c);
void dc_puts(const char *s);
void dc_puthex64(u64 v);
void dc_puthex32(u32 v);
void dc_puthex8(u8 v);
void dc_putdec(u64 v);

#endif
