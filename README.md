# SSM RISC-V Extension

A CV32E40X-based SoC on a MicroPhase A7-Lite (Artix-7 `xc7a35tfgg484-2L`), built
as the platform for a state-space-model accelerator attached over CV-X-IF.

This repository is the working tree for the project: the SoC RTL, the bare-metal
firmware and its build, and the notes from bring-up. The core itself and the
Spike simulator are fetched rather than vendored.

**Status:** SoC runs on hardware. Core fetches, executes and stores; firmware is
verified in simulation before it goes near the board. Serial console is pending a
UART connection (see `doc/BRINGUP.md` §12). The accelerator itself is not started.

---

## Layout

```
rtl/        SoC RTL -- top level, UART transmitter, clock gate shim
fw/         bare-metal firmware, linker scripts, build script
  lib/      UART and GPIO helpers
scripts/    core fetch, serial console
doc/        bring-up log and measurement notes
a7lite_soc/ Vivado project (.xpr + constraints only; runs are regenerated)
blinky/     stage-1 project, kept as the known-good reference bitstream
vendor/     CV32E40X, fetched by scripts/fetch_core.sh  (gitignored)
build/      generated .mem paths for $readmemh            (gitignored)
```

Sources are added to the Vivado project **by reference**, not imported, so
`rtl/` is the only copy and editing it is enough.

---

## Memory map

| Address | |
|---|---|
| `0x0000_0000` – `0x0000_7FFF` | 32 KB unified instruction/data RAM (BRAM) |
| `0x1000_0000` | UART TX data (write byte) |
| `0x1000_0004` | UART status (bit 0 = busy, read-only) |
| `0x1000_0008` | GPIO out (bit 0 → LED2) |

The core boots at `0x0`. There is no MMU and no bus error — an access past the
top of RAM aliases silently onto low memory, which has bitten this project twice.
See `doc/BRINGUP.md` §10.2.

---

## Requirements

- Vivado 2026.1
- xPack `riscv-none-elf-gcc` 13.2.0
- `device-tree-compiler`, if building Spike
- `picocom`, for the serial console

---

## Building from a fresh clone

```bash
# 1. fetch the pinned core revision
./scripts/fetch_core.sh

# 2. build the firmware -- do this BEFORE opening Vivado
cd fw && ./build.sh && cd ..
```

Step 2 is not optional. `build.sh` writes `build/fw_mem_path.svh`, which
`rtl/a7lite_soc_top.sv` includes to find the `.mem` images. That file holds
absolute paths, so it is gitignored and has to be generated locally — without it
elaboration fails with `[HDL 9-3952] use of undefined macro 'FW_MEM_B0'`.

```bash
# 3. open the project, then synthesise, implement and write the bitstream
vivado a7lite_soc/a7lite_soc.xpr
```

**After any firmware change, re-run synthesis, not just implementation.**
`$readmemh` is resolved at elaboration and the contents are baked into the BRAM
`INIT` strings inside the bitstream, so a new `.mem` file changes nothing until
the design is re-elaborated.

---

## Running it

Program the device from the Hardware Manager, then:

```bash
./scripts/console.sh          # finds the port, opens picocom at 115200
```

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
in the RTL.
