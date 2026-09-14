# SSM on CV32E40X — cycle-accurate simulation

Runs the SSM inference on the **CV32E40X soft core** in Verilator: the real RTL,
the same firmware that runs on the A7-Lite FPGA, cycle-accurate, no board needed.

## Run it

```bash
cd ~/a7lite-cv32e40x/verilator

./run.sh bench      # perf counters per kernel   ~90 s
./run.sh gate       # full SSM + golden check    ~7 min
```

Each prints the build, the simulation, and a summary table.

## What it simulates

`a7lite_soc_top` — CV32E40X core + banked BRAM + UART, the same SoC that is
synthesised for the FPGA. The testbench (`tb_main.cpp`) clocks it and decodes
`uart_tx_o` at 115200 baud, so the firmware's own `printf` output comes straight
to the terminal. Firmware is loaded by `$readmemh` from `fw/firmware_b{0..3}.mem`.

Simulation rate is **workload-dependent**: ~820k cycles/s on a short bounded run,
but ~80k cycles/s sustained over the full `gate` workload (33.9 M cycles in 419 s
on an idle machine). Budget from the workload, not from the headline number.
Verilator's default `OPT_FAST=-Os` was A/B'd against `-O2`: 1.7%, so `-O2` is in
`build.sh` and there is no cheap win left there.

## Results

### Correctness — `./run.sh gate`

The full model: `d_model=32, d_state=16, layers=2, timesteps=288`, checked
against `golden.h`.

```
RESULT: 9/9 exact vs golden reference -> PASS
```

### Performance — `./run.sh bench`

| kernel | cycles | instret | IPC | br_taken | ld_stall |
|---|---|---|---|---|---|
| dot_i8 | 2,312 | 1,801 | 0.779 | 255 | 1 |
| gemm_i8 | 5,025 | 4,002 | 0.796 | 511 | 1 |
| **ssm_scan_q15** | **11,201** | **9,669** | **0.863** | 526 | **0** |
| memcpy32 | 199 | 135 | 0.678 | 31 | 1 |

**The SSM recurrence is branch-bound, not memory-bound.** 1,532 stall cycles
against 526 taken branches, with `ld_stall = 0` and `wb_data_stall = 0` — the
memory system keeps up entirely.

> ⚠️ **`ssm_scan_q15` here is a stand-in, not FEMBA's kernel**: int32 state, no
> `d_inner` axis, `b[t][n]*x[t]` in place of a discretised `dB'`, and no
> discretisation at all. The per-element figures below are properties of *that*
> loop. FEMBA's real kernels are in `fw/ssmbench.c` and measure 21.1 instructions
> / 25.6 cycles per element for the scan — `ld_stall = 0` still holds, so the
> branch-bound conclusion survives on the right kernel. See
> `~/Documents/Mamba/phase3-softcore.md`.

That matters for the accelerator design: a fused SSM instruction removes the
inner loop's control flow, and on this core the control flow *is* the stall.

**18.9 instructions and 21.9 cycles per element** (512 elements = 32 timesteps
x 16 state).

## FEMBA's kernels at FEMBA's dimensions — `./run.sh ssmbench`

`fw/ssmbench.c` ports `ssm_discretize_q15` and `ssm_scan_q15` from FEMBA at
`seq_len=80, d_state=16, d_inner=1540` (tiled), and measures the fused form
against the two-kernel form with one variable changed.

| arm | cycles | cycles/element |
|---|---|---|
| discretize | 1,318,363 | 257.5 |
| scan | 131,129 | 25.6 |
| two-kernel | 1,449,506 | 283.1 |
| fused | 1,433,383 | 280.0 |

**Software fusion is worth 1.011×**, bit-exact. Not because fusion is wrong, but
because the fused loop body spills: it removes 2 array stores per element and the
compiler hands back ~1.6 as stack spills. A fused instruction must bring its own
state registers or it reproduces this result.

## Next step

`a7lite_soc_top.sv` already instantiates the CV32E40X **eXtension Interface**,
tied off:

```systemverilog
assign xif.mem_valid = 1'b0;
```

That is where a custom `ssm.step` instruction attaches. The same testbench and
the same firmware then measure the delta — one variable changed.

## Files

| | |
|---|---|
| `build.sh` | verilates the SoC + core, builds `obj_dir/sim` |
| `tb_main.cpp` | clock driver + UART decoder |
| `run.sh` | build firmware, build sim, run, summarise |
| `summarise.py` | formats the UART trace |
| `results/` | saved traces from the runs above |

## Build notes

Verilator **5.052** is required and is installed at `~/verilator5` — the system
4.038 cannot parse CV32E40X's parameterised SystemVerilog interfaces.

`build.sh` excludes `bhv/` and `sva/` (assertion-only; the SoC instantiates
`cv32e40x_core` directly) and substitutes `bhv/cv32e40x_sim_clock_gate.sv` for
`rtl/cv32e40x_clock_gate.sv`, which wraps a Xilinx `BUFGCE` primitive that does
not exist in simulation. **Nothing in `rtl/` or `fw/` is modified.**
