# Bare-metal T8132 kernel -- p.call() payload for m1n1.
#
# Outputs (in build/):
#   kernel.elf   -- linked ELF, useful for disassembly / gdb
#   kernel.bin   -- flat binary; _kentry at offset 0
#   kernel.dump  -- objdump -D of the ELF
#
# Deploy under a live m1n1 session:
#   cd $M1N1/proxyclient
#   ./tools/chainload.py -c $KERNEL/build/kernel.bin
#
# Staging for archival / HTTP delivery on the LAN:
#   make deploy
# ...copies build/kernel.bin to /tmp/m4-serve/kernel-<sha>.bin and
# updates the kernel-latest.bin symlink.

CROSS   ?= aarch64-linux-gnu-
CC      := $(CROSS)gcc
LD      := $(CROSS)ld
OBJCOPY := $(CROSS)objcopy
OBJDUMP := $(CROSS)objdump

BUILD   := build
INCLUDE := include
SERVE   := /tmp/m4-serve

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

GIT_SHA := $(shell (git describe --always --dirty --exclude '*' 2>/dev/null \
                     || git rev-parse --short HEAD 2>/dev/null) | tr -d '\n')

.PHONY: all clean dump deploy

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

deploy: $(BUILD)/kernel.bin
	@mkdir -p $(SERVE)
	@sha="$(GIT_SHA)"; \
	if [ -z "$$sha" ]; then sha="unknown"; fi; \
	dst="$(SERVE)/kernel-$$sha.bin"; \
	cp -f $(BUILD)/kernel.bin "$$dst"; \
	ln -sfn "kernel-$$sha.bin" "$(SERVE)/kernel-latest.bin"; \
	echo "deployed $$dst"; \
	echo "         $(SERVE)/kernel-latest.bin -> kernel-$$sha.bin"

clean:
	rm -rf $(BUILD)
