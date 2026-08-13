# SSM RISC-V Extension

A CV32E40X-based SoC on a MicroPhase A7-Lite (Artix-7 `xc7a35tfgg484-2L`), built
as the platform for a state-space-model accelerator attached over CV-X-IF.

This repository is the working tree for the project: the SoC RTL, the bare-metal
firmware and its build, the PyTorch model development, and the notes from
bring-up. The core itself and the Spike simulator are fetched rather than
vendored.

**Status:** the **week-2 correctness gate is passed**. A trained, quantized
selective SSM (7,781 params, D=32/N=16, 2 layers) runs on the CV32E40X and
reproduces the Python fixed-point reference **bit for bit** on 9 of 9 held-out
ECG beats. Re-verified on hardware in this repo on 2026-08-13
(`results/gate-transplant_20260813_100948.raw.txt`): 9/9 exact, 0 class
disagreements. One inference is 31,903,407 cycles, 0.638 s at 50 MHz, which puts
the core at ~80% duty cycle just to keep up with a beat every ~0.8 s. That is the motivation for the accelerator, which is
the next step and not started. See `STATUS.md`.

---

## Layout

```
rtl/        SoC RTL -- top level, UART transmitter, clock gate shim
fw/         bare-metal firmware, linker scripts, build script
  lib/      UART and GPIO helpers
  ssm_model.c   integer SSM inference, transcribed from py/quantize.py
  model_data.h  exported INT8 weights + scales   (generated, committed)
  golden.h      9 beats + expected logits        (generated, committed)
  gate.c        runs them on-core and self-checks
py/         model development -- fetch, prep, train, quantize, export
              py/venv/ and py/data/ are gitignored (~5.2 GB)
scripts/    build, program, bench, power, UART capture, core fetch
doc/        bring-up log (incl. §13 hard-won facts) and measurement notes
a7lite_soc/ Vivado project -- THIS is the one to open
blinky/     stage-1 project, kept as the known-good reference bitstream
results/    measurement outputs                             (gitignored)
vendor/     CV32E40X, fetched by scripts/fetch_core.sh      (gitignored)
build/      bitstream + generated .mem paths                (gitignored)
```

Sources are added to the Vivado project **by reference**, not imported, so
`rtl/` is the only copy and editing it is enough.

---

## Memory map

| Address | |
|---|---|
| `0x0000_0000` – `0x0001_FFFF` | 128 KB unified instruction/data RAM (BRAM) |
| `0x1000_0000` | UART TX data (write byte) |
| `0x1000_0004` | UART status (bit 0 = busy, read-only) |
| `0x1000_0008` | GPIO out (bit 0 → LED2) |

The core boots at `0x0`. There is no MMU and no bus error — an access past the
top of RAM aliases silently onto low memory, which has bitten this project twice.
See `doc/BRINGUP.md` §10.2.

---

## Requirements

- Vivado 2026.1 — `source /opt/Xilinx/2026.1/Vivado/settings64.sh`, it is not on `PATH`
- xPack `riscv-none-elf-gcc` 13.2.0
- `device-tree-compiler`, if building Spike
- `picocom`, for the serial console
- `py/venv` for anything under `py/` — see `py/README.md` to rebuild it

---

## Building from a fresh clone

```bash
# 1. fetch the pinned core revision
./scripts/fetch_core.sh

# 2. build the firmware -- do this BEFORE opening Vivado
./fw/build.sh          # bench harness, or: main, or: gate
```

Step 2 is not optional. `build.sh` writes `build/fw_mem_path.svh`, which
`rtl/a7lite_soc_top.sv` includes to find the `.mem` images. That file holds
absolute paths, so it is gitignored and has to be generated locally — without it
elaboration fails with `[HDL 9-3952] use of undefined macro 'FW_MEM_B0'`.

```bash
# 3. open the project, then synthesise, implement and write the bitstream
source /opt/Xilinx/2026.1/Vivado/settings64.sh
vivado a7lite_soc/a7lite_soc.xpr
```

Sources are added to that project **by reference** — `rtl/`, `constr/`,
`vendor/cv32e40x/rtl/` and `build/` are all outside the project directory and
edited in place, so there is nothing to re-import after a change. If the RTL
changed, Reset Run on `synth_1` first.

`scripts/create_gui_project.tcl` rebuilds a disposable equivalent into
`vivado_gui/`. That is the rescue path if `a7lite_soc.xpr` is lost, not the
normal way in.

Or without the GUI at all:

```bash
vivado -mode batch -source scripts/build.tcl      # synth + impl + bitstream, ~4 min
vivado -mode batch -source scripts/program.tcl    # configure over JTAG
```

**After any firmware change, re-run synthesis, not just implementation.**
`$readmemh` is resolved at elaboration and the contents are baked into the BRAM
`INIT` strings inside the bitstream, so a new `.mem` file changes nothing until
the design is re-elaborated. **This includes the model weights** — they reach the
FPGA only as BRAM initialisation, via
`model_data.h` → `firmware.elf` → `firmware_b*.mem` → `$readmemh`.

---

## The week-2 gate

The end-to-end correctness check: the same integer arithmetic, run in Python and
on the core, compared logit by logit.

```bash
# host differential check -- no board, no Vivado, ~1 s, 240 beats
gcc -O2 -I fw -I py -DGOLDEN_HEADER='"golden_host.h"' \
    -o /tmp/hc py/host_check.c fw/ssm_model.c && /tmp/hc

# on hardware -- 9 beats
./fw/build.sh gate
vivado -mode batch -source scripts/build.tcl
vivado -mode batch -source scripts/program.tcl
py/venv/bin/python scripts/capture_uart.py --seconds 90 --expect '#GATE'
```

`fw/gate.c` compares on-board and prints `#GATE,PASS` or `#GATE,FAIL`, so no
judgement happens over the UART. Both sides are integer-only — there is no
rounding budget, so anything short of an exact match is a failure.

---

## Running it

Program the device from the Hardware Manager, then:

```bash
./scripts/console.sh          # finds the port, opens picocom at 115200
```

The board presents **two** USB serial devices — an FT232H (`0403:6014`) for JTAG
and a CH340 (`1a86:7523`) for the UART — and which one becomes `/dev/ttyUSB0`
changes between boots, and sometimes mid-session when the CH340 re-enumerates.
Listening on the wrong one reports "no bytes received", which looks exactly like
dead firmware. `console.sh`, `scripts/capture_uart.py` and `scripts/run_bench.py`
all resolve the port by vendor ID by default; see `doc/BRINGUP.md` §13.

Three indicators, deliberately independent so a failure localises itself:

| | Meaning |
|---|---|
| **LED1** (M18) blinking ~1.5 Hz | configuration and clock are alive — does not involve the core |
| **LED2** (N18) toggling ~110 ms | the core is fetching, executing and completing stores |
| **UART** (V2) banner + tick counter | the UART pin and baud divisor are also right |

A tick counter that restarts at 0 means the core is trapping and rebooting
through `mtvec`, not that the count simply wrapped.

The board has no reset input in this design — reset comes from an internal
power-on counter that runs at configuration. To restart, reprogram the device.

---

## Simulation

Firmware is checked in Spike before it goes to hardware. Spike's reset vector is
fixed at `0x1000` with a boot ROM mapped there, so an image linked at `0x0` will
not run. `fw/link_sim.ld` is `fw/link.ld` with `ORIGIN` moved to `0x80000000` and
nothing else changed:

```bash
cd fw
riscv-none-elf-gcc -march=rv32imc_zicsr -mabi=ilp32 -Os \
    -ffreestanding -nostdlib -nostartfiles \
    -T link_sim.ld crt0.S lib/io.c main.c -o firmware_sim.elf -lgcc
spike -d --isa=rv32imc_zicsr firmware_sim.elf
```

---

## Notes

`doc/BRINGUP.md` is the running log of what went wrong and what the cause turned
out to be. Most of the entries looked like a different problem than they were,
which is the reason it exists. Its section numbers are referenced from comments
in the RTL. **§13 is the distilled version** — the facts that cost real time or
would have produced silently wrong results, with no narrative around them.

`STATUS.md` is the handoff document: where the project is, what is decided, and
the three open questions that gate the accelerator RTL. Read it before starting
work in a new session.

`doc/SESSION-2026-08-09.md` is the working log for the day the model was trained,
quantized, ported and passed the gate.
