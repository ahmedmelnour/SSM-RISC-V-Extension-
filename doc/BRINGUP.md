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

The size is written down four times and nothing enforces agreement:

| Where | What |
|---|---|
| `rtl/a7lite_soc_top.sv` | `MEM_WORDS` |
| `fw/build.sh` | `MEM_WORDS` (lines emitted per lane) |
| `fw/link.ld` | `LENGTH` |
| `fw/link_sim.ld` | `LENGTH` — the Spike build, easy to forget |

`link_sim.ld` is the one that got missed during the widening in §10.3: it sat at
32K for three days while everything else was at 128K. It only bites when you next
run Spike, which is exactly when you are debugging something else.

They are all 128 KB — 32768 words — since the widening in §10.3. They started at
32 KB / 8192 words. All three change together. The comment in `build.sh` says as
much and it still drifted apart on me once, so it's worth checking rather than
trusting.

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

### 10.3 Widening 32 KB → 128 KB

32 KB was the whole budget for code *and* data *and* stack, against a model of a
few tens of thousands of INT8 parameters. `bench.c` alone took 6.7 KB; a 20k-param
model was already at the ceiling and 50k did not fit. The gate firmware settles it:
20,597 B of text plus 19,551 B of `.bss` is 40 KB before the stack is touched, so
it cannot run in 32 KB at all.

`MEM_WORDS` went 8192 → 32768 in all three places from §10.1. Verified three ways
rather than just synthesised:

- 32 RAMB36 exactly (64% of the 50 on this part), as predicted. LUTs went *down* 9.
- WNS **+2.360 ns** against the 32 KB build's +1.660 ns. No timing cost — the
  critical path was never the memory address decode. Treat that as placement noise
  as much as a real gain; it is not a new ceiling either way.
- On hardware all five `bench.c` kernels reproduced the 32 KB numbers
  **bit-identically** — same cycles, instret, loads, stores (`results/mem128k_*`).
  That is what keeps measurements from before the widening comparable with
  everything after it.

Budget now: 128 KB for code + data + stack, 18 RAMB36 spare for the accelerator.

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

It works. Synthesis infers four true dual-port BRAMs with no `8-6841` warning —
4 × (8K × 8) and 8 RAMB36 at the original 32 KB, and 4 × (32K × 8) and 32 RAMB36
after the widening in §10.3.

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

> **Resolved.** This section describes the state before the board's own UART
> connector was cabled. Once it was, a second USB serial device appeared — a
> CH340 (`1a86:7523`) — and the console works. What replaced the original problem
> is a worse one, because it fails silently: with *two* USB serial devices on the
> bus, `/dev/ttyUSB0` is no longer reliably the UART. See §13.

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

## 13. Hard-won facts

Each of these cost real time or would have produced silently wrong results. They
came out of the work up to the week-2 gate; §1–§12 above are the bring-up log
proper, this is the residue that does not belong to any one section.

### Toolchain and Vivado

**Vivado shows zero parts if the licence has an Alveo feature.** `~/.Xilinx/*.lic`
must not contain `INCREMENT Vivado_Alveo_Package`; it restricts device support to
Alveo and makes `get_parts` return 0 — which looks exactly like missing board
files. Already stripped. Check with `puts [llength [get_parts -quiet]]`, expect 289.

**There is no Vivado board file for the A7-Lite.** Select the *part*
(`xc7a35tfgg484-2L`) directly. This is permanent, not a setup fault. Don't go
looking in the Vivado Store.

**`vivado` is not on `PATH` in a fresh shell.** Everything needs
`source /opt/Xilinx/2026.1/Vivado/settings64.sh` first. If you redirect the log,
`vivado: command not found` looks exactly like a build that ran and produced
nothing — while a *stale* `build/a7lite_soc.bit` sits there ready to be programmed.
`scripts/run_bench.py` is immune: it invokes Vivado by absolute path.

**The vendor clock gate is simulation-only.** `vendor/cv32e40x/bhv/cv32e40x_sim_clock_gate.sv`
models the gate with an `always_latch` and infers a latch on a gated clock net.
`rtl/cv32e40x_clock_gate.sv` replaces it with a BUFGCE under the same module name,
and `bhv/` is deliberately not in the project. `build.tcl` fails the build if any
latch is inferred, so a wrong pickup cannot ship silently.

### Pinout

**`UART_TX` = `V2`, confirmed empirically.** An earlier attempt moved it to `U2`,
which is an FPGA *input* driven by the CH340 — driving it causes bus contention.
The manual separately lists a flash at "Position U2"; that is a component
designator and has nothing to do with FPGA ball U2. Do not conflate them.

Clock `J19` (50 MHz) · `UART_TX` `V2` · `UART_RX` `U2` · LED1 `M18` · LED2 `N18`.

**7-series needs `CFGBVS` and `CONFIG_VOLTAGE`** in the XDC or implementation
fails DRC `NSTD-1`/`UCIO-1`. Both are set in `constr/a7lite.xdc`.

**`/dev/ttyUSB0` is not necessarily the UART.** The board presents *two* USB
serial devices: an FT232H (`0403:6014`) for JTAG and a CH340 (`1a86:7523`) for the
UART. Which gets `ttyUSB0` depends on enumeration order, so it changes between
boots and between cable orders. Capturing on the FT232H gives
`RESULT: SILENT -- no bytes received`, which looks exactly like dead firmware, a
wrong baud or a bad pin constraint — and costs a full reprogram cycle to rule out,
because the firmware prints once at reset and there is no reset button. Resolve by
vendor ID rather than trusting the number:

```bash
for d in /sys/bus/usb-serial/devices/*; do
    n=$(basename "$d")
    udevadm info -q property -n /dev/$n | grep -q 1a86 && echo "UART is /dev/$n"
done
```

**This is not hypothetical and it is not only a per-boot problem.** On 2026-08-12
the CH340 re-enumerated mid-session — USB device number 058 → 059 — and its tty
moved from `ttyUSB0` to `ttyUSB1` with nothing else changing. A capture that had
worked minutes earlier died with `FileNotFoundError: /dev/ttyUSB0`. Had the FT232H
happened to hold a tty at that moment, it would have been worse: a clean `SILENT`
result on the wrong device, indistinguishable from dead firmware.

`scripts/capture_uart.py` and `scripts/run_bench.py` now default to `--port auto`,
which walks `/sys/class/tty/*/device` up to the USB node and matches `idVendor`
against `1a86`. `scripts/console.sh` prefers the CH340 the same way and warns when
it has to fall back. Pass `--port` explicitly only to override.

### Measurement

**CV32E40X disables all performance counters out of reset.** `mcountinhibit`
resets to "all inhibited" to save power, so `mcycle`/`minstret` read a frozen
constant until software clears it — indistinguishable from a broken harness.
`perf_init()` handles it, and the firmware echoes the register back so a run can
never be silently invalid.

**`mhpmevent3` is an event *bitmask*, not an index.** Multiple bits OR together
and there is at most one increment per cycle, so it is not a sum. Use one event
per counter if you want exact counts.

**Two processes reading the same tty split the byte stream between them.** Running
`run_bench.py` and `capture_uart.py` against the same port at once does not give
each a copy — the kernel hands each byte to whichever reader asks first, so both
get a shredded subset. It does not look like a collision: it looks like corrupted
output from a broken design. Real example, one line from each capture of the same
run:

```
#CFG,mcountinhi,expect,got,exact      <- two lines with chunks stolen mid-stream
#RESULT,exact,9,o   +   f,9           <- "#RESULT,exact,9,of,9" torn in half
```

Both captures also showed `RESULT: OK -- found '#GATE'`, so neither reported a
problem. One reader at a time.

**`--expect '#GATE'` matches `#GATE,FAIL` as happily as `#GATE,PASS`.** The capture
script only reports whether the *marker* arrived; the verdict is a separate string.
Read the `#RESULT` and `#GATE` lines, don't trust the `RESULT: OK` summary line —
that one means "the UART works", not "the gate passed".

**Code layout moves the cycle count by ~6% at constant instruction count.** The same
`gate.c`, same `-O2`, same core, same clock, compiled once with and once without
`-ffunction-sections -fdata-sections`:

| | with | without |
|---|---|---|
| cycles | 31,903,407 | 33,874,576 |
| instret | 27,156,479 | 27,155,608 |
| IPC | 0.851 | 0.802 |

Instruction count identical to 0.003%, cycles **5.8% apart**, and each is
reproducible to the digit on its own build — so it is deterministic, not noise. The
cause is instruction-fetch alignment: `-ffunction-sections` relocates every
function, and with RVC a hot loop starting on a 4-byte rather than a 2-byte
boundary changes what the alignment buffer has to do. Over 27M instructions of
tight scan loops that is ~2M cycles.

This matters well beyond the gate: **a 6% swing can be manufactured by a compiler
flag that has nothing to do with the accelerator.** Compile the baseline and the
accelerated build identically, record the flags in every results `.meta`, and treat
any speedup under ~1.1× as inside layout noise.

**Calibrate overhead through the exact path you measure through, and per event.**
Getting this wrong biased every event count by exactly +20 instructions. The sweep
measures `instret` twice by independent means specifically so that class of bug
shows up as a disagreement — keep that redundancy.

### Model and quantization

**A hardcoded shift constant sized for a worst case that never occurs is invisible
until the scales around it get tight — then it dominates.** Three of them in
`py/quantize.py` (`SV=5` in the LayerNorm variance, an unscaled `isqrt(var)`,
`SP=6` in the scan) were harmless under a single global activation scale and became
the *largest* error source the moment scales went per-tensor: implementing
per-tensor scales first made float-vs-int agreement go **down**, 62% → 53%. Fixing
the three constants took it to **97%** — worth far more than every activation scale
put together. All three are now derived from `D`, `N` and the int32 bound. When a
quantization change makes things worse, suspect the fixed shifts before the scales.

**~99% is the ceiling with INT8 weights**, not a target to push past. Per-channel
INT8 with *exact* activations measures 98.9%; the remaining gap is weight
precision, not scales.

**If saturation goes up when you add range, stop adding range.** Saturation counts
are usually a symptom of upstream precision loss inflating values, not of the range
being too small. `--headroom 1` pushed `y` saturation from 32% to 41%; the real
fault was the LayerNorm variance shift. After fixing that, saturations went
3.3M → **0** with no headroom at all.

**Cheap in operations is not the same as cheap in bits.** A final LayerNorm before
the head is ~0.01% of the op budget and never touches the scan — and it grew the
residual stream **4.4×** (50.7 → 221.4), because the head reading the pooled vector
directly is the only thing making weight decay pay for an unbounded stream.
Agreement fell 97.3% → 96.4% and per-channel weight scales stopped helping at all.
Reverted. The op-budget table is the right tool for deciding what to *accelerate*
and the wrong tool for deciding what is safe to *add*.

> Residue of that revert: `py/data/model_v2.pt` still carries the `norm_out.*`
> keys, so loading it into the current `ECGNet` raises "Unexpected key(s) in
> state_dict". `py/test_quantize.py` used to default to it and silently never ran.

**Indexing `t` out of a `[B,T,D,N]` tensor inside a scan makes backward 14× forward.**
The first `SelectiveSSM.forward` precomputed `a` for all timesteps then used
`a[:, t]` in the loop. Forward 0.74 s, backward 10.56 s — each `select` backward
allocates and zeros a gradient buffer the size of the *whole* tensor (67 MB at
batch 256), 128 times per layer. Calling `.unbind(1)` once lowers it to a single
stack in backward: 0.42 s, and an epoch went from ~1825 s to ~116 s. If the scan
ever feels inexplicably slow again, look for a `[:, t]` on a large tensor first.

**Never pass `--threads` above 4 to `py/train.py`.** At 8 threads training is
**31× slower** while pegging every core. A run left at `os.cpu_count()` took five
hours to reach epoch 2 of 20.

**A rare class can stay flat until the LR anneals.** The S class read as pure
noise through epoch 14, then jumped 12× at epoch 15. Do not conclude a class is
unlearnable before the schedule finishes.

---

## Quick reference

```bash
./fw/build.sh                    # bench + .mem + build/fw_mem_path.svh
./fw/build.sh gate               # the week-2 correctness gate (needs the model)
ls -la --time-style=+%H:%M fw/*.mem fw/gate.c    # .mem must be newer than sources
spike -d --isa=rv32imc_zicsr firmware_sim.elf    # simulator (link_sim.ld build)
./scripts/console.sh             # serial console
py/venv/bin/python scripts/capture_uart.py --seconds 90 --expect '#GATE'
```

After changing firmware, re-run synthesis — not just implementation.
