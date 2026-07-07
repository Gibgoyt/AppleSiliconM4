#ifndef KERNEL_H
#define KERNEL_H

#include "types.h"
#include "boot_args.h"

#define KERNEL_NAME     "hello-t8132"
#define KERNEL_VERSION  "0.1"

void kmain(struct boot_args *ba);

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
