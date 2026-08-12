# Bring-up notes

Running log of things that went wrong during bring-up and what the cause turned
out to be. I'm writing these down because most of them cost me more time than
they should have, and nearly all of them looked like a different problem than
they were.

The section numbers are referenced from comments in the RTL, so don't renumber
them.

---

## 1. Toolchain

xPack riscv-none-elf-gcc 13.2.0, unpacked to `~/tools/xpack-riscv-none-elf-gcc-13.2.0-2`.
Not on the default PATH, so `~/.bashrc` has:

```bash
export PATH=$HOME/tools/xpack-riscv-none-elf-gcc-13.2.0-2/bin:$PATH
```

`fw/build.sh` doesn't rely on that. It builds the tool paths from `$CROSS` with a
default pointing at the same directory, so the build works from a shell that
hasn't sourced anything.

---

## 2. Spike needs device-tree-compiler

Building Spike failed at configure:

```
checking for dtc... no
configure: error: device-tree-compiler not found
make: *** No targets specified and no makefile found.  Stop.
```

`sudo apt install device-tree-compiler` fixes it. Spike simulates a whole
machine, not just a CPU, and it generates a device tree describing that machine
at startup — so it needs `dtc` to compile it.

The second line is worth noting because it sent me looking in the wrong place.
There was no Makefile because configure aborted before writing one. The `make`
error is a consequence, not a separate fault. General rule I keep relearning:
when "no makefile found" follows a configure run, scroll up.

---

## 3. Spike can't run an image linked at address 0

My `link.ld` puts RAM at `0x0` because that's where the core boots (`BOOT_ADDR`).
Spike won't run that. Two things in Spike are fixed and can't be overridden from
the command line:

- the reset PC is `0x00001000` (`riscv/platform.h`)
- a boot ROM device is mapped at that same address (`riscv/sim.cc`)

Spike starts at `0x1000`, runs a small ROM stub, then jumps to the ELF entry
point. The entry address itself is free, but a 32 KB RAM based at `0x0` overlaps
that boot ROM. There's no `--pc` flag to move it.

Fix: keep `link.ld` at `0x0` for the FPGA, and keep a second `link_sim.ld` that
differs only in `ORIGIN`, set to `0x80000000` (Spike's default DRAM base):

```bash
riscv-none-elf-gcc ... -T link_sim.ld crt0.S main.c -o firmware_sim.elf
spike -d --isa=rv32imc_zicsr firmware_sim.elf
```

Everything the simulator is there to check is unaffected by the move — `sp`, `gp`,
the `.bss` zeroing, the result in memory. Only the base address changes. This is
also why `_stack_top` is written as `ORIGIN(RAM) + LENGTH(RAM)` and not as a
literal: relocating the image is one edit and the two numbers can't drift.

Spike prints `warning: tohost and fromhost symbols not in ELF; can't communicate
with target` on every run. It's a warning, it runs anyway. It means the firmware
has no host-communication channel, which is what bare metal means. Test suites
built for Spike define `tohost` to report pass/fail; I don't have one.

---

## 4. Getting the .mem paths into the RTL

`$readmemh` needs a filename, and the RTL refers to it through `` `FW_MEM_B0 ``..
`` `FW_MEM_B3 ``. If nothing defines those, Vivado reports:

```
[HDL 9-3952] use of undefined macro 'FW_MEM_B0'
```

once per fileset — `sources_1` and `sim_1` are separate, so anything done through
Vivado's Verilog defines has to be done twice.

I went with a generated header instead. `fw/build.sh` writes
`build/fw_mem_path.svh` at the end of every build, and the RTL just includes it.
The paths are regenerated alongside the files they point at, so they can't go
stale, and there's no project state to reapply.

**The paths must be absolute.** `$readmemh` resolves a relative path against the
process working directory, which is `<project>.runs/synth_1/` for synthesis and
somewhere under `<project>.sim/` for simulation. Neither is the project root.
`build.sh` derives the directory from `BASH_SOURCE`, so they come out absolute
without hardcoding anything.

### 4.1 A missing .mem file is only a warning

This is the dangerous one. If `$readmemh` can't open the file, it warns, leaves
the array uninitialised, and elaboration carries on. On hardware the BRAM comes
up zeroed, so the core fetches `0x00000000`, traps immediately, and there's no
obvious clue why.

So after every synthesis run I grep the log for `readmemh` before believing
anything. Same habit as the byte-lane cross-check: verify the output, don't just
check that the tool exited cleanly.

---

## 5. The bitstream contains the firmware from *elaboration time*

Changing `firmware_b*.mem` does nothing on its own. `$readmemh` is resolved when
the design elaborates, and the contents end up baked into the BRAM `INIT` strings
inside the bitstream. A new `.mem` needs a full re-synthesis, not just
implementation.

I got caught by this. I rewrote `main.c` for the UART bring-up, built the
bitstream, and the board ran the old sum-to-100 test program from Stage 3 —
because I never re-ran `build.sh`, so the `.mem` files were 15 minutes older than
the `main.c` I'd just written. Nothing warned me. The symptom was LED1 blinking,
LED2 dark and the UART silent.

Two checks that would have caught it in seconds:

```bash
ls -la --time-style=+%H:%M fw/*.mem fw/main.c   # .mem must be newer
grep -c 10000000 fw/firmware.dis                # must be > 0 for a UART build
```

Note what my own LED scheme would have told me here: "LED2 dark means the core
isn't completing stores." That diagnosis would have been wrong. The core was
fine; it was running a program that does no I/O. The indicators test the path and
assume the firmware exercises it.

For Stage 5, when I'll be swapping benchmarks repeatedly, look at `updatemem`
with an `.mmi` — it patches memory contents into an existing `.bit` in seconds
instead of a full re-run.

---

## 6. Freestanding link errors

Two separate ones, both from `-nostdlib`.

**Missing `lib/io.c`.** `build.sh` had `SRCS="crt0.S $PROG.c"` and `io.c` defines
`uart_puts` and friends as real functions, not inline in the header. It compiles
fine and fails at link with `undefined reference to 'uart_puts'`. Added `lib/io.c`
to `SRCS`.

**Missing `__udivdi3` / `__umoddi3`.** `uart_put_u64` divides a 64-bit value.
RV32 has no 64-bit divide instruction, so GCC lowers it into calls to libgcc
helpers — and `-nostdlib` excludes libgcc along with libc. Fixed with `-lgcc`
after the objects on the link line. That brings in the compiler's arithmetic
helpers only, not libc.

This will matter in Stage 5: `mcycle` is 64-bit and printing it goes straight
down this path.

---

## 7. `--gc-sections` was doing nothing

`-Wl,--gc-sections` had been in `LDFLAGS` from the start, but the linker can only
discard whole sections, and by default every function lands in one `.text`. It
needs `-ffunction-sections -fdata-sections` on the compile side to have anything
to discard.

With both, the unused `uart_put_u64`, `uart_put_hex32` and `uart_flush` drop out
of the image — `firmware.map` lists only the five functions `main` actually
reaches. Worth having noticed: as written, the flag looked like it was doing work
and wasn't.

---

## 8. Two tools writing firmware.map

`LDFLAGS` had `-Wl,-Map=firmware.map`, and the script later ran
`nm -n firmware.elf > firmware.map`. `nm` ran last, so it silently destroyed the
linker map on every build, and what was left was a perfectly valid file with
plausible content.

Renamed the linker map to `firmware.ld.map`. It's the one that shows section
placement and sizes, which is what I'd want for checking whether the stack has
grown into `.bss`. The `nm` output only gives symbol addresses.

---

## 9. Vivado project hygiene

`build.sh` wasn't executable and `./build.sh` gave `Permission denied`. That's
the execute bit, not file ownership — the shebang is irrelevant until the kernel
is allowed to exec the file. `chmod +x` fixed it. (`bash build.sh` works without
the bit, because then bash is the program and the script is just input.)

The SoC project was pulling its constraints from
`blinky/blinky.srcs/constrs_1/new/a7lite.xdc` — the *other* project's directory.
It worked, but deleting or regenerating `blinky/` would have silently stripped
the pin assignments. Copied the XDC into `a7lite_soc.srcs/constrs_1/new/` and
repointed the fileset.

Pins, for reference: J19 clock (50 MHz, 20 ns), M18 LED1, N18 LED2, V2 UART TX,
all LVCMOS33.

---

## 10. Memory sizing and address aliasing

### 10.1 Three places define the memory size

The size is written down three times and nothing enforces agreement:

| Where | What |
|---|---|
| `rtl/a7lite_soc_top.sv` | `MEM_WORDS` |
| `fw/build.sh` | `MEM_WORDS` (lines emitted per lane) |
| `fw/link.ld` | `LENGTH` |

They're currently all 32 KB — 8192 words. If I ever widen the BRAM, all three
change together. The comment in `build.sh` says as much and it still drifted
apart on me once, so it's worth checking rather than trusting.

### 10.2 The aliasing incident

The address decode is `addr[AW_HI:2]` with `AW_HI` derived from `MEM_WORDS`.
There is no range check and no bus error — anything above the top of RAM silently
wraps onto low memory.

An earlier version of the SoC indexed an 8192-entry array with `addr[16:2]` and
aliased. I hit the same class of bug a second time from the other direction:
`link.ld` was set to 128 KB while the RTL was still 8192 words (32 KB), so
`_stack_top` came out at `0x20000`, past the end of the real memory.

What made it interesting is that it appeared to work. The first push lands near
`0x1FFFC`, and truncating that to 13 bits gives index 8191 — the top word of the
real 32 KB. Because 128 KB / 32 KB is exactly 4, the top of the imagined space
aliases precisely onto the top of the real space, so the stack ended up in the
right place *by coincidence*. Change either size to a non-power-of-four ratio and
that stops being true.

The part that isn't lucky: the linker believed it had 128 KB. Anything placed
above `0x8000` would link without complaint and then land on top of the reset
vector at address 0.

This is why the index width is derived from `MEM_WORDS` in the RTL rather than
written out by hand — the array and the slice that addresses it can't disagree.
The remaining exposure is the linker script, which the hardware has no way to
check.

---

## 11. Byte-lane memory images

The RAM is four byte-wide arrays rather than one 32-bit array with byte enables.
With a single `MEM_WORDS x 32` array Vivado reports

```
[Synth 8-6841] ... cannot take advantage of ByteWide feature and is implemented
with single write enable per RAM
```

because the word address exceeds its byte-write-enable threshold of 12 bits. It
then slices the RAM into 4-bit-wide primitives with one write enable each, which
can't express a byte write at all — so `sb`/`sh` would corrupt the neighbouring
bytes of the word. Splitting the lanes explicitly makes every write enable a
whole-RAM enable by construction.

It works. Synthesis infers 4 × (8K × 8) true dual-port BRAMs, 8 RAMB36 total, no
`8-6841` warning.

`build.sh` splits the binary little-endian into `firmware_b0..b3.mem`. The check
that catches a byte-order mistake immediately:

```bash
head -1 firmware.mem                                  # 30401073
for l in 0 1 2 3; do head -1 firmware_b$l.mem; done   # 73 10 40 30
```

Reading the same word two ways. If the lanes come out reversed the core fetches
garbage and I'd be debugging the SoC instead of the script.

---

## 12. Serial console

There is no CH340 on the USB bus. The only FPGA-related device is

```
0403:6014  FTDI FT232H  — product string "Digilent USB Device"
```

which is the JTAG programmer. It has a single USB interface and is driven over
libusb by Vivado's hw_server, which is why it isn't bound to `ftdi_sio` and never
produces a `/dev/ttyUSB*`. FT232H is single-channel, so there's no spare UART on
it. The `uart_tx_o` pin (V2) needs either the board's own UART connector on a
second cable, or an external USB-TTL adapter.

`scripts/console.sh` finds the port and opens picocom at 115200. When no port
exists it says which of the causes it is instead of `FATAL: cannot open`.

**brltty.** It's installed on this machine and it claims CH340 devices as braille
displays. The symptom is that `/dev/ttyUSB0` appears and then vanishes a few
seconds later, which looks exactly like a flaky cable. `sudo apt remove brltty`.

Worth remembering that the UART is the only one of the three bring-up indicators
that depends on any of this. LED1 and LED2 are independent, so the core can be
verified with no serial connection at all — which is the entire reason the scheme
has three separate indicators.

---

## Quick reference

```bash
cd fw && ./build.sh              # firmware + .mem + build/fw_mem_path.svh
ls -la --time-style=+%H:%M fw/*.mem fw/main.c    # .mem must be newer than sources
spike -d --isa=rv32imc_zicsr firmware_sim.elf    # simulator (link_sim.ld build)
./scripts/console.sh             # serial console
```

After changing firmware, re-run synthesis — not just implementation.
