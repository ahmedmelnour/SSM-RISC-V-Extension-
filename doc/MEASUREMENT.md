# Measurement infrastructure

How cycles, instructions, hardware events and power are measured on this platform,
and what each number is and is not allowed to claim.

Everything here is scripted, because these get run hundreds of times and the failure
mode of doing it by hand is measuring a stale bitstream.

---

## 1. The trap that must be dealt with first

**CV32E40X disables all performance counters out of reset.**

```systemverilog
//  Note: implemented counters are disabled out of reset to save power
mcountinhibit_q <= MCOUNTINHIBIT_MASK; // default disable
```
<sub>`vendor/cv32e40x/rtl/cv32e40x_cs_registers.sv`</sub>

Until software clears `mcountinhibit` (`0x320`), `mcycle` and `minstret` read back a
frozen constant. Every kernel appears to take the same number of cycles, which looks
exactly like a broken harness rather than a disabled counter.

`perf_init()` clears it, and `bench.c` reports the register back as
`#CFG,mcountinhibit,<value>`. `run_bench.py` prints a loud warning if it is non-zero,
so a run can never be silently invalid.

---

## 2. Counter map

Taken from the RTL, not from the generic RISC-V spec:

| CSR | Address | Notes |
|---|---|---|
| `mcycle` / `mcycleh` | `0xB00` / `0xB80` | always implemented, gated by `mcountinhibit[0]` |
| `minstret` / `minstreth` | `0xB02` / `0xB82` | always implemented, gated by `mcountinhibit[2]` |
| `mhpmcounter3` / `h` | `0xB03` / `0xB83` | present when `NUM_MHPMCOUNTERS >= 1` |
| `mhpmevent3` | `0x323` | **event bitmask, not an event index** |

`mhpmevent3` selects events by *bit position*: write `1 << n`. The counter increments
in any cycle where

```systemverilog
|(hpm_events & mhpmevent_rdata[...])
```

is true — so setting several bits counts *cycles in which any of them fired*, at most
one increment per cycle. **It is not a sum.** Select one event at a time for exact counts.

### Available events

| Bit | Name | Meaning |
|---|---|---|
| 0 | `cycle` | always 1 |
| 1 | `instret` | retired instructions |
| 2 | `compressed` | retired compressed (RVC) instructions |
| 3 | `jump` | unconditional jumps |
| 4 | `branch` | conditional branches |
| 5 | `branch_taken` | conditional branches taken |
| 6 | `intr_taken` | interrupts taken (excl. NMI) |
| 7 | `data_read` | OBI data-side read transactions |
| 8 | `data_write` | OBI data-side write transactions |
| 9 | `if_invalid` | IF had no valid output when ID was ready |
| 10 | `id_invalid` | ID had no valid output when EX was ready |
| 11 | `ex_invalid` | EX had no valid output when WB was ready |
| 12 | `wb_invalid` | WB had no valid output |
| 13 | `ld_stall` | load-use hazards |
| 14 | `jalr_stall` | jump-register hazards |
| 15 | `wb_data_stall` | WB stall cycles caused by loads/stores |

Events 7, 8, 13 and 15 are the ones that bear on an accelerator argument: they quantify
memory traffic and the stalls it causes, which is precisely what a tightly-coupled unit
is supposed to remove.

---

## 3. Why only one event counter

The SoC is built with `NUM_MHPMCOUNTERS = 1`, so there is a single selectable event
counter. Kernels are therefore **re-run once per event**.

This is deliberate, and sound here:

- The kernels are fully deterministic — no cache, no branch predictor, no interrupts, no
  DRAM refresh. Re-running a kernel to observe a different event yields identical
  execution. The harness confirms this: `cycles` and `instret` come out bit-identical
  across every event pass for a given kernel, which is a free self-check.
- Each `mhpmcounter` is 64 bits wide plus an event mux over 16 signals. **The design has
  only ~9% timing margin at 50 MHz** (WNS +1.660 ns), so widening the counter file is
  not free, and burning margin on measurement plumbing would compromise the thing being
  measured.

If you later need simultaneous capture, raise `NUM_MHPMCOUNTERS` in
`rtl/a7lite_soc_top.sv` and re-check WNS before trusting any timing comparison.

---

## 4. Overhead calibration

Reading `mcycle` costs cycles. For short kernels that is not negligible, so `perf_init()`
measures an empty kernel and takes the minimum over 8 trials. `perf_measure*()` subtracts
the floor, saturating at zero.

Two rules, both learned the hard way on the first run:

**Calibrate through the exact path you measure through.** The first version calibrated
cycles and instructions through a lighter path than the one that also reads the event
counter. Result: the `nop` kernel reported 4 cycles instead of ~0, and every event count
was biased high by a constant. There is now a single `raw_measure()` used by both
calibration and measurement.

**The event floor is per-event, not a single constant.** With `instret` selected, the
counter sees the harness's own CSR reads; with `data_write` selected it sees almost
nothing. A global constant would bias some events and over-correct others. `perf_init()`
therefore calibrates all 16 events separately, and the floors used are emitted as `#OVH`
lines so a reader can audit the correction instead of trusting it.

### The self-check that catches this class of bug

The sweep measures `instret` **twice by independent means** — once from the `minstret` CSR
and once from `mhpmcounter3` with the `instret` event selected. They must agree.

On the first run they differed by *exactly 20* on all five kernels:

| kernel | `minstret` | `instret` event | Δ |
|---|---|---|---|
| nop | 2 | 22 | 20 |
| dot_i8 | 1803 | 1823 | 20 |
| gemm_i8 | 4004 | 4024 | 20 |
| ssm_scan_q15 | 9671 | 9691 | 20 |
| memcpy32 | 137 | 157 | 20 |

A constant offset across kernels of wildly different sizes is the signature of a harness
artefact rather than a real effect. Keep both columns in the sweep — the redundancy is
what makes the harness self-validating. **If they ever diverge again, distrust the
harness before distrusting the hardware.**

Likewise: a `nop` row that is not approximately zero means the calibration is wrong and
every other row is suspect.

---

## 5. Guarding against measuring nothing

Every kernel folds its result into a `volatile int32_t checksum`, which is printed with
each row. Without this, `-O2` is entitled to delete a pure-computation kernel entirely as
dead code, and you would be timing an empty loop at a very impressive IPC.

Working sets live in `.bss` and are filled at startup by a deterministic LCG rather than
being static initialised data. `.bss` is `NOLOAD` and zeroed by `crt0.S`, so several KB of
buffers cost nothing in the bitstream image.

---

## 6. Running a measurement

```bash
./scripts/run_bench.py --tag baseline
```

One command covering: build firmware → synthesise → implement → program → capture → parse.

| Flag | Use |
|---|---|
| `--no-bit` | firmware unchanged in a way that matters? *No* — see warning below |
| `--capture-only` | board already programmed and running |
| `--opt -O3` | change optimisation level (recorded in the results metadata) |
| `--prog main` | build a different program |
| `--tag <name>` | label the results file |

> **The firmware is baked into the BRAM initialisation.** Any firmware change requires a
> full Vivado rebuild. `--no-bit` after editing a kernel will measure the *old* code while
> appearing to succeed. This is the single easiest way to produce wrong numbers, and is
> why the default path always rebuilds.

### Outputs

`results/<tag>_<timestamp>.{csv,meta,raw.txt}`

The `.meta` file records git revision, whether the tree was dirty, compiler flags,
compiler version, and post-route utilisation — so any number can be traced back to what
produced it. A dirty tree is recorded rather than blocked, but a PPA claim should rest on
a clean one.

### Wire format

The firmware emits only `#`-prefixed lines, so stray output can never be parsed as data:

```
#PERF,1
#CFG,key,value
#COLS,kernel,reps,cycles,instret,ev_id,ev_name,ev_count,checksum
#DATA,ssm_scan_q15,1,12345,6789,13,ld_stall,42,0x1a2b3c4d
#END
```

The reader starts **before** programming and waits for `#END` rather than for a fixed
window, because the harness prints immediately out of reset.

---

## 7. Power

Three approaches, in ascending order of how much they can be claimed.

### 7.1 Vectorless estimation — relative comparison only

```bash
vivado -mode batch -source scripts/report_power.tcl
```

No switching activity data; Vivado assumes a default toggle rate. Current baseline:

| Metric | Value |
|---|---|
| Total on-chip | 0.081 W |
| Dynamic | 0.009 W |
| Device static | 0.072 W |
| Confidence | Medium |

**Static power dominates by 8×.** That is the important methodological fact on this part —
see §7.4.

The script pins reset nets to "deasserted" before estimating. Without that, the vectorless
engine assumes the high-fanout reset toggles like any other net and is asserted ~50% of the
time, which suppresses flop activity and triggers `[Power 33-332]`.

> Reset nets are found **structurally**, via the `R`/`CLR`/`PRE`/`S` pins they drive — not
> by name. Synthesis renamed `rst_n` to `u_core_n_57`, so any name pattern silently matches
> nothing and the correction is skipped without complaint. Note those pins are all
> *active high*, so the net driving them is the inverse of `rst_n` and its static
> probability is ~0, not ~1.

### 7.2 SAIF-driven estimation — what a PPA claim should rest on

```bash
vivado -mode batch -source scripts/report_power.tcl -tclargs ssm_kernel path/to/run.saif
```

Real per-net switching activity from a simulation of the actual kernel. This is the number
to put in the report. Producing the SAIF means running the kernel in `xsim` against the
post-synthesis or post-implementation netlist and calling `write_saif` — worth setting up
before the coupling sweep, since every sweep point needs one.

### 7.3 Board current measurement

**The A7-Lite is powered entirely from the +5 V USB rail** ("The board is use the +5V supply
from USB") and has **no on-board current-sense shunt**. So the practical options are:

- **Inline USB power meter** on the programming/power cable. Cheapest and adequate for
  coarse deltas; typical resolution ~10 mA (~50 mW at 5 V).
- **Bench supply + ammeter** feeding `VCC_5V` on the expansion header (JP1/JP2 pin 11).
  Better resolution, but requires care not to backfeed the USB host.

### 7.4 The honest limitation — read this before planning the PPA section

Board-level measurement sees the **whole board**: DDR3, Gigabit Ethernet PHY, USB PHY, QSPI
flash, CH340, and the regulators' own losses. The FPGA core is a small fraction of that, and
within the FPGA, static power already outweighs dynamic by roughly 8×.

An accelerator that changes dynamic power by a few milliwatts is therefore very unlikely to
be resolvable with a ~10 mA USB meter against a multi-hundred-milliamp board baseline.

**Practical consequence for the FYP:**

- Use **SAIF-driven Vivado estimation** as the primary power metric, reported as dynamic
  power at a fixed clock so the accelerator's contribution is visible.
- Use **board current** only as a coarse sanity check that nothing pathological is happening.
- Report **energy per inference** (dynamic power × measured cycles ÷ frequency) rather than
  power alone — it combines the two things that are actually measured well here, and it is
  the metric that a coupling sweep should move.
- State the estimation method and confidence level next to every power number.

---

## 8. Files

| Path | Role |
|---|---|
| `fw/lib/perf.h` / `perf.c` | CSR access, 64-bit reads, overhead calibration |
| `fw/lib/io.h` / `io.c` | UART output, integer formatting |
| `fw/bench.c` | kernels + sweep + wire format |
| `scripts/run_bench.py` | full flow, parsing, results + metadata |
| `scripts/report_power.tcl` | vectorless or SAIF power estimation |
| `results/` | timestamped outputs (gitignored) |

---

## 9. Kernels

| Kernel | Purpose |
|---|---|
| `nop` | harness floor; must read ≈ 0 after overhead subtraction |
| `dot_i8` | INT8 dot product, 256 elements — MAC-bound roofline anchor |
| `gemm_i8` | INT8 GEMM 8×8×8 — the declared fallback kernel |
| `ssm_scan_q15` | Q15 selective state-space scan, T=32 N=16 — **the target kernel** |
| `memcpy32` | pure load/store loop — isolates memory behaviour from arithmetic |

`ssm_scan_q15` implements the real recurrence with per-timestep coefficients:

```
h[n] = (a[t][n]*h[n] + b[t][n]*x[t]) >> 15
y[t] = sum_n (c[t][n]*h[n]) >> 15
```

Coefficients vary per timestep — that is what makes the scan *selective*, and what prevents
the compiler hoisting them out of the loop. This is the baseline the CV-X-IF instruction has
to beat, and the `ld_stall` / `data_read` counts on this kernel are the quantitative
justification for building it.
