#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Build the firmware and emit the four byte-lane images for $readmemh.
#
#   ./build.sh            # builds main.c
#   OPT=-O3 ./build.sh    # override optimisation level
#
# The optimisation level is part of the measurement: it changes the instruction
# mix, so it is recorded in build_info.txt next to the artefacts.
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PROG="${1:-main}"
OPT="${OPT:--Os}"

if [ ! -f "$PROG.c" ]; then
    echo "no such program: $PROG.c" >&2
    exit 1
fi

CROSS="${CROSS:-$HOME/tools/xpack-riscv-none-elf-gcc-13.2.0-2/bin/riscv-none-elf}"
CC="${CROSS}-gcc"
OBJCOPY="${CROSS}-objcopy"
OBJDUMP="${CROSS}-objdump"
SIZE="${CROSS}-size"
NM="${CROSS}-nm"

# CV32E40X is RV32IMC with Zicsr.
ARCH="-march=rv32imc_zicsr -mabi=ilp32"
# -ffunction-sections/-fdata-sections put every function and object in its own
# section, which is what makes the linker's --gc-sections able to drop unused
# ones. Without them --gc-sections is close to a no-op: everything is in one
# .text and the linker can only discard whole sections.
CFLAGS="$ARCH $OPT -g -ffreestanding -fno-builtin -Wall -Wextra -I. \
-ffunction-sections -fdata-sections"
LDFLAGS="-nostdlib -nostartfiles -Wl,--gc-sections -Wl,-Map=firmware.ld.map -T link.ld"

# RV32 has no 64-bit divide instruction, so GCC lowers the u64 division in
# uart_put_u64 into calls to __udivdi3/__umoddi3. Those live in libgcc, which
# -nostdlib also excludes -- hence -lgcc, placed after the objects that need it.
LIBS="-lgcc"

# lib/io.c holds uart_puts/led_set as real functions, not inline in io.h, so it
# has to be compiled in -- omitting it fails at link, not at compile.
SRCS="crt0.S lib/io.c $PROG.c"

echo "[fw] program=$PROG opt=$OPT"
# shellcheck disable=SC2086
"$CC" $CFLAGS $LDFLAGS $SRCS -o firmware.elf $LIBS

echo "[fw] objcopy"
"$OBJCOPY" -O binary firmware.elf firmware.bin

echo "[fw] generating memory images"
python3 - <<'PY'
import struct

with open("firmware.bin", "rb") as f:
    blob = f.read()

if len(blob) % 4:
    blob += b"\x00" * (4 - len(blob) % 4)

MEM_WORDS = 8192   # must match MEM_WORDS in the SoC top and LENGTH in link.ld
                   # 8192 words * 4 B = 32 KB
words = struct.unpack("<%dI" % (len(blob) // 4), blob)
if len(words) > MEM_WORDS:
    raise SystemExit(
        "firmware is %d words (%d bytes), exceeds the %d-word BRAM"
        % (len(words), len(blob), MEM_WORDS))

# The SoC RAM is four byte-wide arrays, so emit one image per byte lane.
# Lane n holds bits [8n+7 : 8n] of each word.
for lane in range(4):
    with open("firmware_b%d.mem" % lane, "w") as f:
        for i in range(MEM_WORDS):
            w = words[i] if i < len(words) else 0
            f.write("%02x\n" % ((w >> (8 * lane)) & 0xFF))

with open("firmware.mem", "w") as f:
    for i in range(MEM_WORDS):
        f.write("%08x\n" % (words[i] if i < len(words) else 0))

print("[fw] %d words, %d bytes, %.1f%% of BRAM"
      % (len(words), len(blob), 100.0 * len(words) / MEM_WORDS))
PY

"$SIZE" firmware.elf
"$OBJDUMP" -d firmware.elf > firmware.dis
"$NM" -n firmware.elf > firmware.map

# Recorded so a results file can always be traced back to what produced it.
{
    echo "program=$PROG"
    echo "opt=$OPT"
    echo "arch=$ARCH"
    echo "cc=$("$CC" -dumpversion)"
    echo "built=$(date -Is)"
} > build_info.txt

# The RTL takes the .mem paths from this generated header rather than from a
# Vivado -D define, so the paths are regenerated with the files they point at and
# cannot go stale. Absolute, because $readmemh resolves relative paths against the
# synth/sim run directory, not the project root.
#
# The name must stay in step with the `include in rtl/a7lite_soc_top.sv.
mkdir -p ../build
{
    echo '`ifndef FW_MEM_PATH_SVH'
    echo '`define FW_MEM_PATH_SVH'
    for l in 0 1 2 3; do
        echo "\`define FW_MEM_B$l \"$HERE/firmware_b$l.mem\""
    done
    echo '`endif'
} > ../build/fw_mem_path.svh

echo "[fw] done -> firmware_b{0..3}.mem, firmware.dis, build_info.txt, ../build/fw_mem_path.svh"
