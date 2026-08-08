#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build the bring-up firmware and emit firmware.mem for $readmemh.
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

CROSS="${CROSS:-$HOME/tools/xpack-riscv-none-elf-gcc-13.2.0-2/bin/riscv-none-elf}"
CC="${CROSS}-gcc"
OBJCOPY="${CROSS}-objcopy"
OBJDUMP="${CROSS}-objdump"
SIZE="${CROSS}-size"

# CV32E40X is RV32IMC with Zicsr. -Os keeps the image inside 32 KB with room to spare.
CFLAGS="-march=rv32imc_zicsr -mabi=ilp32 -Os -g -ffreestanding -fno-builtin \
        -Wall -Wextra -nostdlib -nostartfiles -Wl,--gc-sections -Wl,-Map=firmware.map"

echo "[fw] compiling"
"$CC" $CFLAGS -T link.ld crt0.S main.c -o firmware.elf

echo "[fw] objcopy"
"$OBJCOPY" -O binary firmware.elf firmware.bin

echo "[fw] generating memory images"
python3 - <<'PY'
import struct

with open("firmware.bin", "rb") as f:
    blob = f.read()

# Pad to a whole number of 32-bit words.
if len(blob) % 4:
    blob += b"\x00" * (4 - len(blob) % 4)

MEM_WORDS = 8192
words = struct.unpack("<%dI" % (len(blob) // 4), blob)
if len(words) > MEM_WORDS:
    raise SystemExit("firmware is %d words, exceeds %d-word BRAM" % (len(words), MEM_WORDS))

# The SoC RAM is four byte-wide arrays (see a7lite_soc_top.sv), so emit one
# image per byte lane. Lane n holds bits [8n+7 : 8n] of each word.
# Every entry is written so the BRAM initialisation is fully specified rather
# than leaving the tail X in simulation.
for lane in range(4):
    with open("firmware_b%d.mem" % lane, "w") as f:
        for i in range(MEM_WORDS):
            w = words[i] if i < len(words) else 0
            f.write("%02x\n" % ((w >> (8 * lane)) & 0xFF))

# Word-wide image kept for inspection and for any future simulation testbench.
with open("firmware.mem", "w") as f:
    for i in range(MEM_WORDS):
        f.write("%08x\n" % (words[i] if i < len(words) else 0))

print("[fw] %d instruction/data words, %d bytes -> firmware_b{0..3}.mem" % (len(words), len(blob)))
PY

"$SIZE" firmware.elf
"$OBJDUMP" -d firmware.elf > firmware.dis
echo "[fw] done -> firmware.mem, firmware.dis"
