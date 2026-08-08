# Project status & handoff

**Read this first in a new session.** It carries the context needed to continue without
re-deriving anything. Last updated **2026-08-09** (~day 2 of a 10-week FYP).

```
Repo:      ~/a7lite-cv32e40x         (git, 5 commits, clean)
Board:     MicroPhase A7-Lite, Artix-7 xc7a35tfgg484-2L, connected & working
Vivado:    /opt/Xilinx/2026.1        (BASIC license, 289 parts)
Toolchain: ~/tools/xpack-riscv-none-elf-gcc-13.2.0-2/bin/riscv-none-elf-gcc
```

---

## 1. The project

Final-year project, ~10 weeks / ~480 hrs, started ~2026-08-08. Supervisor Dr Jeevan,
lab VeCAD.

**Goal:** a tightly-coupled RISC-V custom instruction for selective state-space
(SSM / Mamba-style) inference at the edge — CV32E40X core + CV-X-IF extension
interface, FPGA prototype, synthesis-level PPA.

**Three claimed contributions:**
1. The instruction and its microarchitecture.
2. A real verification methodology (coverage + corner cases, weeks 7–8) — **explicitly
   never to be cut.**
3. A coupling sweep (timesteps-per-instruction) to find the crossover point.

**Hard gate, end of week 2 (~2026-08-21):** is the quantized SSM running correctly in
software on the core? If **no**, switch the kernel to INT8 GEMM immediately and do not
spend week 3 rescuing it. The rest of the plan is unchanged except the kernel;
verification and coupling contributions survive the swap intact.

---

## 2. Where we are

| Step | Budget | Status |
|---|---|---|
| **1. Core bring-up** | days 1–3 | ✅ **complete** |
| **2. Measurement infra** | days 4–5 | ✅ **complete**, one gap (SAIF) |
| **3. SSM in software** | days 6–12 | ❌ **not started** — critical path |

Roughly **3 days ahead of schedule**, ~12 days to the gate.

### Step 1 ✅
- CV32E40X builds and runs on hardware. Never needed the Ibex fallback, so CV-X-IF is retained.
- Hello-world over UART: banner + 313 gap-free ticks from reset.
- GCC toolchain compiles and loads; firmware image is baked into BRAM init.
- `mcycle` working (see the `mcountinhibit` trap in §5).

### Step 2 ✅ (with one gap)
- Cycle harness, validated: two independent counters agree exactly, `nop` reads 0.
- Instruction profiling: `minstret` + 16 hardware events.
- Reusable scripts: `run_bench.py` is one command for build→synth→program→capture→parse.
- ⚠️ **Gap: the SAIF power flow is documented but not built.** Vectorless estimation works
  today; SAIF is what a defensible PPA claim needs, and every coupling-sweep point will
  need one. Not blocking Step 3, but must not drift.

### Step 3 ❌ not started

> **`ssm_scan_q15` in `fw/bench.c` is NOT a model.** It is a synthetic benchmark kernel
> with LCG-random coefficients — no task, no trained weights, no accuracy, no Python
> reference. It exists to measure the *shape* of the computation, and it did that job.
> Do not count it as Step 3 progress.

---

## 3. Decisions made

| Decision | Choice | Why |
|---|---|---|
| Core | CV32E40X (not Ibex) | brought up cleanly; keeps the CV-X-IF standard extension interface |
| **Step 3 task** | **MIT-BIH ECG anomaly detection** | see below |
| Reset | on-chip power-on counter, no button | manual's reset key "K3" absent from its own key table |
| RAM | four byte-wide lanes | Vivado drops byte write-enables above a 12-bit address (§5) |
| `NUM_MHPMCOUNTERS` | 1, events multiplexed across re-runs | kernels are deterministic; only ~9% timing margin to spend |

**Why ECG over keyword spotting:** KWS needs an MFCC/filterbank frontend running on the
core. That is FFT-heavy and would likely dominate the cycle budget — you would be
accelerating the SSM while the frontend eats the gains, confounding the measurement.
ECG feeds raw or minimally-filtered samples straight into the scan, so **the SSM is the
workload**. Dataset is also ~100 MB rather than ~2 GB.

---

## 4. How to run everything

```bash
cd ~/a7lite-cv32e40x

./fw/build.sh bench                                   # firmware (or: main)
vivado -mode batch -source scripts/build.tcl          # synth+impl+bitstream (~15 min)
vivado -mode batch -source scripts/program.tcl        # configure over JTAG
python3 scripts/capture_uart.py --seconds 6           # read UART

./scripts/run_bench.py --tag baseline                 # all of the above, parsed
vivado -mode batch -source scripts/report_power.tcl   # power estimate

vivado -mode batch -source scripts/create_gui_project.tcl   # makes vivado_gui/a7lite_soc.xpr
```

> **The firmware is baked into the BRAM initialisation.** Any firmware change needs a full
> Vivado rebuild. `--no-bit` after editing a kernel silently measures the *old* code. This
> is the easiest way to produce wrong numbers.

**Layout:** `rtl/` SoC · `fw/` firmware (+`fw/lib/` io+perf) · `constr/` XDC ·
`scripts/` build/program/bench/power · `vendor/cv32e40x` upstream pinned at `d952cd6`
(gitignored, restore with `scripts/fetch_core.sh`) · `build/`, `results/`, `vivado_gui/`
generated · `doc/BRINGUP.md` full walkthrough incl. GUI equivalents ·
`doc/MEASUREMENT.md` counter map and power methodology.

---

## 5. Hard-won facts — do not re-derive these

Each of these cost real time or would have produced silently wrong results.

**Vivado shows zero parts if the licence has an Alveo feature.** `~/.Xilinx/*.lic` must not
contain `INCREMENT Vivado_Alveo_Package`; it restricts device support to Alveo and makes
`get_parts` return 0, which looks exactly like missing board files. Already stripped. Check
with `puts [llength [get_parts -quiet]]` — should be 289.

**There is no Vivado board file for the A7-Lite.** Select the *part* directly. Permanent,
not a setup fault. Don't search the Vivado Store.

**Pinout (from the reference manual, verified on hardware):**
clock `J19` 50 MHz · `UART_TX` `V2` · `UART_RX` `U2` · LED1 `M18` · LED2 `N18`.
`UART_TX` = `V2` is **confirmed empirically**. An earlier attempt wrongly moved to `U2`;
`U2` is an FPGA *input* driven by the CH340, so driving it causes bus contention. The
manual separately lists a flash at "Position U2" — a component designator, unrelated to
FPGA ball U2.

**The vendor clock gate is simulation-only.** `bhv/cv32e40x_sim_clock_gate.sv` infers a
latch. `rtl/cv32e40x_clock_gate.sv` replaces it with a BUFGCE using the same module name.
`build.tcl` fails the build if any latch is inferred, so a wrong pickup can't ship silently.

**Vivado silently drops byte write-enables above a 12-bit address.** This design needs 13.
It then slices the RAM into 4-bit-wide primitives with one write enable each, which cannot
express a byte write — every `sb` would corrupt the rest of the word. Hence four explicit
byte-wide arrays. Watch for `[Synth 8-6841]`.

**CV32E40X disables all performance counters out of reset.** `mcountinhibit` resets to
"all inhibited" to save power, so `mcycle`/`minstret` read a frozen constant until software
clears it — indistinguishable from a broken harness. `perf_init()` handles it; the firmware
echoes the register back so a run can never be silently invalid.

**`mhpmevent3` is an event *bitmask*, not an index.** Multiple bits OR together, max one
increment per cycle — it is not a sum. One event per counter for exact counts.

**Calibrate overhead through the exact path you measure through**, and per-event. Getting
this wrong biased every event count by exactly +20 instructions. The sweep measures
`instret` twice by independent means specifically so that class of bug shows up as a
disagreement — keep that redundancy.

---

## 6. Baseline measurements (2026-08-09, `-O2`, 50 MHz)

| kernel | cycles | instret | IPC | loads | stores |
|---|---|---|---|---|---|
| `nop` | 0 | 0 | – | 0 | 0 |
| `memcpy32` | 199 | 135 | 0.678 | 33 | 1 |
| `dot_i8` | 2312 | 1801 | 0.779 | 513 | 1 |
| `gemm_i8` | 5025 | 4002 | 0.796 | 1025 | 1 |
| `ssm_scan_q15` | 11201 | 9669 | 0.863 | 2084 | 532 |

**`ssm_scan_q15` (T=32, N=16): 21.9 cycles and 18.9 instructions per inner iteration to
perform ~8 ops of real arithmetic — ~58% of the instruction stream is addressing and loop
overhead.** That is the quantified headroom the CV-X-IF instruction has to reclaim.

**Utilisation / timing:** 3733 LUTs (18%), 2304 FF (5.5%), 8 RAMB36 (16%), 3 DSP.
**WNS +1.660 ns → Fmax ≈ 54.5 MHz, only ~9% margin at 50 MHz.** The bare core is already
near the limit on this part; the accelerator must stay off the critical path or the clock
comes down. Fix the frequency deliberately before the coupling sweep, or PPA comparisons
are confounded.

**Power (vectorless):** total 0.081 W, dynamic 0.009 W, static 0.072 W — **static dominates
8:1**. The board runs entirely off USB +5 V with no current-sense shunt, so a few mW of
accelerator dynamic power will not be resolvable with a ~10 mA USB meter against the whole
board (DDR3, Ethernet PHY, regulators). Use SAIF-driven estimation as the primary metric,
board current as a coarse sanity check, and report **energy per inference**.

---

## 7. Two platform issues to resolve early

### 7.1 ⚠️ 32 KB of RAM will not hold the model — fix this first

The SoC has **32 KB total for code + data + stack**. The plan targets "tens of thousands of
parameters":

| Model size | INT8 weights |
|---|---|
| 10k params | 9.8 KB |
| 20k params | 19.5 KB |
| 50k params | 48.8 KB |

`bench.c` alone is already 6.7 KB. A 20k-parameter model is **at or over the ceiling**, and
50k does not fit at all.

**Fix — plenty of headroom on the part:** currently 8 of 50 RAMB36 are used. Going to
**128 KB needs 32 RAMB36 and still leaves 18 spare.** Change `MEM_WORDS` in
`rtl/a7lite_soc_top.sv` from 8192 to 32768 and widen the word index from `addr[14:2]` to
`addr[16:2]` (`AW_HI` is already computed from `MEM_WORDS`, so check it follows), update
`link.ld` `LENGTH`, and the `MEM_WORDS` constant in `fw/build.sh`. **Re-check WNS after** —
a wider address decode eats into that 9% margin.

Do this before porting the model, not after.

### 7.2 There is no memory bottleneck to remove — reframe the sweep

`ld_stall` and `wb_data_stall` measure **0 on every kernel**. That is *correct*: the BRAM is
zero-wait-state and structurally cannot stall (`gnt = req`, `rvalid` next cycle).

Consequence: any speedup measured here comes **entirely** from collapsing instruction count,
and is a **lower bound** versus a realistic memory system where an accelerator also hides
memory latency. An examiner will notice.

**Suggested fix that turns it into a contribution:** make data-memory wait states a
parameter and run the coupling sweep at 0/1/2/4 wait states. "Crossover as a function of
memory latency" is a stronger result than a single crossover number, for ~30 lines of RTL.
Decide before building the accelerator — it changes what the sweep measures.

---

## 8. Next actions — Step 3, MIT-BIH ECG

Environment gaps (checked 2026-08-09): Python 3.10, numpy 1.21, scipy 1.8, matplotlib 3.5
present. **Missing: torch, torchaudio, sklearn, wfdb, librosa. No datasets on disk.**

1. **Widen SoC memory to 128 KB** (§7.1) and confirm WNS still positive.
2. `python3 -m venv ~/a7lite-cv32e40x/py/venv`; install `torch` (CPU wheel is fine for a
   model this size), `wfdb`, `scikit-learn`, `matplotlib`.
3. Fetch MIT-BIH via `wfdb.dl_database('mitdb', ...)`. Frame the task — beat classification
   (AAMI classes) is the conventional framing; confirm with Dr Jeevan whether he wants
   classification or anomaly detection.
4. Train a small S4/S6-style model. Target **≤10k parameters** given §7.1 — suggested shape:
   input projection → 2 SSM layers (state N=16) → pooling → linear head.
5. Quantize to INT8 weights, **INT16 state** (state matrices are quantization-sensitive —
   the plan already flags this as the fallback; consider starting there).
6. Validate accuracy in PyTorch, float vs quantized.
7. **Write a fixed-point Python reference** using integer ops only. This is the artefact the
   C port must match bit-for-bit — *not* the float model. Report quantized-vs-float accuracy
   separately. Getting this ordering right is what makes the bit-exactness claim meaningful.
8. Export weights to a C header; port inference to C; run bare-metal.
9. Add the real kernel to `fw/bench.c` alongside `ssm_scan_q15`, so the synthetic proxy and
   the real model are measured side by side.
10. **Gate check (~21 Aug):** correct on hardware? If not, switch to INT8 GEMM immediately.

### Also outstanding
- Build the SAIF power flow (xsim testbench + `write_saif`) — §2 gap.
- Decide the wait-state question (§7.2) before accelerator design.
- Fix the target clock frequency for all PPA comparisons (§6).

---

## 9. Housekeeping

- A Vivado **GUI** instance (PID 2698, started 01:29) is Ahmed's own, holding ~3.2 GB and
  idle, with the *old* `~/projects/cv32e40x_soc` project open. Not part of this repo. Safe
  to close after checking for unsaved work.
- Old attempts under `~/projects/`, `~/vivado_projects/`, `~/parent/`, `~/cv32e40x*` were
  abandoned mid-debug and contain broken RTL. **Do not restart from them.**
