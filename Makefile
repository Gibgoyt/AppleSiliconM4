# Bare-metal T8132 kernel -- top-level build.
#
# Outputs (in build/):
#   kernel.elf   -- linked ELF, useful for disassembly / gdb
#   kernel.bin   -- flat binary for chainload.py -r -E 0
#   kernel.dump  -- objdump -D of the ELF
#
# Deploy under a live m1n1 session:
#   cd $M1N1/proxyclient
#   ./tools/chainload.py -r -E 0 $KERNEL/build/kernel.bin

CROSS   ?= aarch64-linux-gnu-
CC      := $(CROSS)gcc
LD      := $(CROSS)ld
OBJCOPY := $(CROSS)objcopy
OBJDUMP := $(CROSS)objdump

BUILD   := build
INCLUDE := include

CFLAGS  := -Wall -Wextra -Werror \
           -O2 -g \
           -ffreestanding -fno-stack-protector -fno-pic \
           -mgeneral-regs-only -mstrict-align \
           -mcmodel=small \
           -std=gnu11 \
           -I$(INCLUDE)

ASFLAGS := -g -I$(INCLUDE)

LDFLAGS := -nostdlib -static -T linker.ld --no-dynamic-linker

C_SRCS  := $(wildcard src/*.c)
S_SRCS  := $(wildcard src/*.S)
OBJS    := $(patsubst src/%.c,$(BUILD)/%.o,$(C_SRCS)) \
           $(patsubst src/%.S,$(BUILD)/%.o,$(S_SRCS))

.PHONY: all clean dump

all: $(BUILD)/kernel.bin $(BUILD)/kernel.dump

$(BUILD):
	mkdir -p $@

$(BUILD)/%.o: src/%.c | $(BUILD)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD)/%.o: src/%.S | $(BUILD)
	$(CC) $(ASFLAGS) -c $< -o $@

$(BUILD)/kernel.elf: $(OBJS) linker.ld | $(BUILD)
	$(LD) $(LDFLAGS) $(OBJS) -o $@

$(BUILD)/kernel.bin: $(BUILD)/kernel.elf
	$(OBJCOPY) -O binary $< $@

$(BUILD)/kernel.dump: $(BUILD)/kernel.elf
	$(OBJDUMP) -D $< > $@

dump: $(BUILD)/kernel.dump

clean:
	rm -rf $(BUILD)
