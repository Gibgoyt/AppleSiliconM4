#ifndef KERNEL_BOOT_ARGS_H
#define KERNEL_BOOT_ARGS_H

#include "types.h"

#define CMDLINE_LENGTH_RV3 1024

struct boot_video {
    u64 base;
    u64 display;
    u64 stride;
    u64 width;
    u64 height;
    u64 depth;
};

struct boot_args {
    u16 revision;
    u16 version;
    u64 virt_base;
    u64 phys_base;
    u64 mem_size;
    u64 top_of_kernel_data;
    struct boot_video video;
    u32 machine_type;
    void *devtree;
    u32 devtree_size;
    char cmdline[CMDLINE_LENGTH_RV3];
    u64 boot_flags;
    u64 mem_size_actual;
};

#endif
