#ifndef KERNEL_H
#define KERNEL_H

#include "types.h"

#define KERNEL_NAME     "m4-payload"
#define KERNEL_VERSION  "0.3"

/*
 * kmain -- payload entry, invoked by p.call() from Python.
 *
 * The current M2 signature is (log_buf, log_size, ba). Later phases
 * (NIC bring-up, TCP server, ...) will change the trailing args as
 * needed; log_buf/log_size stay because we always want output.
 */
int  kmain(u64 log_buf, u64 log_size, u64 ba);

__attribute__((noreturn))
void panic(const char *msg);

static inline void hang(void)
{
    for (;;) __asm__ volatile("wfe");
}

static inline u64 read_currentel(void)
{
    u64 v;
    __asm__ volatile("mrs %0, CurrentEL" : "=r"(v));
    return (v >> 2) & 3;
}

static inline u64 read_mpidr(void)
{
    u64 v;
    __asm__ volatile("mrs %0, MPIDR_EL1" : "=r"(v));
    return v;
}

static inline void mmio_write32(u64 addr, u32 val)
{
    *(volatile u32 *)addr = val;
}

static inline u32 mmio_read32(u64 addr)
{
    return *(volatile u32 *)addr;
}

#endif
