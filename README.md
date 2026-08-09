# CV32E40X on MicroPhase A7-Lite

Bring-up of the OpenHW Group [CV32E40X](https://github.com/openhwgroup/cv32e40x) core
on a MicroPhase A7-Lite (Artix-7 `XC7A35T-2FGG484`, Vivado part `xc7a35tfgg484-2L`).

This is step 1 of the FYP: get the unmodified core fetching, executing and talking to a
host over UART, so that the CV-X-IF coprocessor port has a known-good platform to plug
an SSM accelerator into later.

> **Continuing this project in a new session? Start with [STATUS.md](STATUS.md)** —
> current status against the plan, hard-won gotchas, baseline numbers, and next actions.

## Layout

```
rtl/                    SoC RTL written for this project
  a7lite_soc_top.sv     top level: core + BRAM + UART + GPIO
  uart_tx.sv            8N1 transmitter
  cv32e40x_clock_gate.sv  BUFGCE gate replacing the vendor simulation-only model
fw/                     bring-up firmware (crt0.S, main.c, link.ld, build.sh)
constr/a7lite.xdc       pin constraints, from the A7-Lite reference manual
scripts/
  build.tcl             non-project synth -> impl -> bitstream
  program.tcl           JTAG configuration
  capture_uart.py       read the CH340 back-channel and classify the result
vendor/cv32e40x/        upstream core, pinned at d952cd6
build/                  generated: bitstream, checkpoints, reports
```

## Build and run

```bash
./fw/build.sh                                          # firmware -> fw/firmware.mem
vivado -mode batch -source scripts/build.tcl           # -> build/a7lite_soc.bit
vivado -mode batch -source scripts/program.tcl         # configure over JTAG
python3 scripts/capture_uart.py --seconds 6            # read the banner
```

The firmware image is baked into the BRAM initialisation, so changing firmware means
re-running both `fw/build.sh` and `scripts/build.tcl`.

## Memory map

| Address | Width | Description |
|---|---|---|
| `0x0000_0000`–`0x0001_FFFF` | 128 KB | unified instruction/data RAM (BRAM) |
| `0x1000_0000` | w | UART TX data (write a byte to send) |
| `0x1000_0004` | r | UART status, bit 0 = busy |
| `0x1000_0008` | rw | GPIO out, bit 0 drives LED2 |

## Board facts worth not re-deriving

Taken from the *A7-LITE Reference Manual* (Microphase FPGA_DOC V1.0), not from
community examples:

| Signal | Pin | Note |
|---|---|---|
| 50 MHz clock | `J19` | oscillator U10 |
| LED1 | `M18` | D6 |
| LED2 | `N18` | D5 |
| UART_TX | `V2` | "UART data output", FPGA → CH340 |
| UART_RX | `U2` | "UART data input", CH340 → FPGA |

- **There is no Vivado board file for the A7-Lite.** Create projects by part, and write
  constraints by hand. This is permanent, not a setup fault.
- The manual lists a QSPI flash at "Position U2". That is a *component designator* and
  has nothing to do with FPGA ball `U2`. Conflating the two leads to assigning the UART
  transmitter to the FPGA's receive pin.
- The manual's reset section mentions a key "K3" that does not appear in its own key
  table (which lists only KEY1=`AA1`, KEY2=`W1`). This design therefore does not use a
  reset button at all — reset is generated on-chip by a power-on counter.

## Debugging by elimination

The three indicators are deliberately independent, so a failure narrows itself:

| LED1 (M18) | LED2 (N18) | UART | Conclusion |
|---|---|---|---|
| dark | dark | — | configuration or clock problem; check DONE |
| blinks | dark | — | clock fine, core not executing — suspect BRAM init or boot address |
| blinks | blinks | silent | core runs; the UART **pin** is wrong |
| blinks | blinks | garbage | pin right, **baud** divisor wrong |
| blinks | blinks | banner | working |

`DONE = 1` proves configuration succeeded. It does **not** prove the pin assignments are
right — that is exactly the failure mode LED2 exists to rule out.

## Notes on the core

- `bhv/` is excluded from synthesis. It contains `cv32e40x_sim_clock_gate.sv`, which
  models the gate with an `always_latch` and is explicitly marked as unusable for FPGA
  synthesis. `rtl/cv32e40x_clock_gate.sv` here is a `BUFGCE`-based replacement with the
  same module name and ports. `build.tcl` asserts that zero latches were inferred, so
  the wrong file being picked up fails the build rather than producing a broken bitstream.
- CV-X-IF is instantiated and tied off to "always reject" with `X_EXT = 0`. The port is
  present and correctly typed, ready for the accelerator phase.

## Measurement infrastructure

Cycle, instruction and hardware-event measurement is scripted end to end:

```bash
./scripts/run_bench.py --tag baseline        # build -> synth -> program -> capture -> parse
vivado -mode batch -source scripts/report_power.tcl
```

See [doc/MEASUREMENT.md](doc/MEASUREMENT.md) for the counter map, the
`mcountinhibit` trap that freezes all counters out of reset, and what each power
number is allowed to claim.

### Baseline (2026-08-09, -O2, 50 MHz)

| kernel | cycles | instret | IPC | loads | stores |
|---|---|---|---|---|---|
| `nop` | 0 | 0 | - | 0 | 0 |
| `memcpy32` | 199 | 135 | 0.678 | 33 | 1 |
| `dot_i8` | 2312 | 1801 | 0.779 | 513 | 1 |
| `gemm_i8` | 5025 | 4002 | 0.796 | 1025 | 1 |
| `ssm_scan_q15` | 11201 | 9669 | 0.863 | 2084 | 532 |

`ssm_scan_q15` (T=32, N=16) is the target kernel: 21.9 cycles and 18.9
instructions per inner iteration to perform roughly 8 ops of real arithmetic, so
**~58% of the instruction stream is addressing and loop overhead**. That is the
headroom a CV-X-IF instruction is meant to reclaim.

**`ld_stall` and `wb_data_stall` are 0 on every kernel, and that is correct**: the
BRAM is zero-wait-state and structurally cannot stall. There is no memory
bottleneck on this platform for an accelerator to remove, so any speedup measured
here is a lower bound relative to a realistic memory system.

## Bring-up result (verified 2026-08-09)

Hardware, not simulation: `xc7a35t_0` via Digilent `210241416548`, DONE = 1.

```
=====================================
 CV32E40X alive on MicroPhase A7-Lite
 RV32IMC @ 50 MHz, 32KB BRAM
=====================================
tick 0
tick 1
tick 2
...
```

313 consecutive ticks captured from reset with zero gaps. `UART_TX` = `V2` is
confirmed correct on hardware.

| Metric | Value |
|---|---|
| LUTs | 3733 / 20800 (18%) |
| Registers | 2304 / 41600 (5.5%) |
| RAMB36 | 8 / 50 (16%) |
| DSP48 | 3 / 90 |
| WNS / WHS | +1.660 ns / +0.084 ns |

**Headroom note:** WNS of +1.660 ns at a 20 ns period implies Fmax ≈ 54.5 MHz, only
about 9% margin. The core alone is already close to the limit at 50 MHz on this part,
so an accelerator on the CV-X-IF port needs to stay off the critical path or the clock
will have to come down.

If a UART capture shows dropped lines, check whether JTAG programming was running at
the same time — the FT232H and the CH340 share USB bus 003, and contention drops bytes
host-side. A capture taken on its own is gap-free.
