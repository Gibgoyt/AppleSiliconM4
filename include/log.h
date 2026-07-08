#ifndef KERNEL_LOG_H
#define KERNEL_LOG_H

#include "types.h"

/*
 * Payload log. We write into a caller-supplied byte buffer whose
 * (address, size) come in as kmain args from Python. The host reads
 * the buffer back after p.call() returns and prints it as ASCII.
 *
 * Why not just write to the dockchannel MMIO directly? While m1n1
 * is resident and serving its proxy protocol, the dockchannel UART
 * is shared: our raw bytes collide with framed proxy frames and get
 * discarded by the client. The memory-buffer detour is the reliable
 * way to get output out while m1n1 stays alive.
 *
 * All log_put* helpers are bounds-checked; overruns are silently
 * dropped so a payload that produces too much text doesn't corrupt
 * surrounding memory. The buffer is kept null-terminated after each
 * call, so the host can treat it as a C string.
 */

void log_init(u64 buf, u64 size);

void log_putc(char c);
void log_puts(const char *s);
void log_puthex64(u64 v);
void log_puthex32(u32 v);
void log_puthex8(u8 v);
void log_putdec(u64 v);

#endif
