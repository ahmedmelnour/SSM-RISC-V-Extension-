# Project status & handoff

**Read this first in a new session.** It carries the context needed to continue without
re-deriving anything. Last updated **2026-08-12**.

```
Repo:      ~/ssm_riscv_extension    git, remote ahmedmelnour/SSM-RISC-V-Extension-
Archive:   ~/a7lite-cv32e40x        the tree the gate was originally passed in.
                                    Read-only reference. Do not resume work there.
Board:     MicroPhase A7-Lite, Artix-7 xc7a35tfgg484-2L, connected & working
Vivado:    /opt/Xilinx/2026.1   BASIC license, 289 parts.
                                `source /opt/Xilinx/2026.1/Vivado/settings64.sh` first --
                                vivado is NOT on PATH (doc/BRINGUP.md §13)
Toolchain: ~/tools/xpack-riscv-none-elf-gcc-13.2.0-2/bin/riscv-none-elf-gcc
Python:    py/venv/bin/python   torch 2.13, wfdb, sklearn (bootstrap: py/README.md)
```

**Checkpoints in `py/data/` (gitignored — back them up outside git).**
`model_d32n16_run3.pt` is run #3, DS2 0.5596 — **still the best model**.
`model_v3.pt` is the isolation run and **the one `fw/model_data.h` was exported from**.
`model_v2.pt` is the two-variable run (DS2 0.5071); it still carries the reverted final
LayerNorm, so it will not load into the current `ECGNet`. `model_d16n64.pt` is the
architecture comparison. `model.pt` is superseded by `model_d32n16_run3.pt`.

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

**Hard gate, end of week 2 (~2026-08-21): ✅ PASSED 2026-08-09, twelve days early.**
The quantized SSM runs on the CV32E40X and reproduces `py/quantize.py` **bit for bit** on
9 of 9 held-out beats — `#GATE,PASS` in `results/gate_20260809_225054.txt`. The INT8-GEMM
fallback is **not needed**; the selective-SSM kernel stands.

Bit-exact means exactly that: both sides are integer arithmetic, so there is no rounding
budget and the logits match to the digit. Verified three ways — 240/240 beats on the host,
identical results at `-O0/-Os/-O2/-O3`, clean under UBSan — before anything was synthesised.

---

## 2. Where we are

| Step | Budget | Status |
|---|---|---|
| **1. Core bring-up** | days 1–3 | ✅ **complete** |
| **2. Measurement infra** | days 4–5 | ✅ **complete**, one gap (SAIF) |
| **3. SSM in software** | days 6–12 | ✅ **complete — gate passed 2026-08-09** |
| **4. CV-X-IF instruction** | — | 🔴 **critical path, not started** |

Steps 1–3 are complete with ~12 days of budget left over. `doc/SESSION-2026-08-09.md` is
the full working log for Step 3.

**The critical path is now Step 4.** Three questions in §5 gate the RTL and none of them
is answered yet.

---

## 3. How to run everything

```bash
cd ~/ssm_riscv_extension

./fw/build.sh                                         # bench (or: main, gate)
vivado -mode batch -source scripts/build.tcl          # synth+impl+bitstream (~15 min)
vivado -mode batch -source scripts/program.tcl        # configure over JTAG
py/venv/bin/python scripts/capture_uart.py --seconds 6

./scripts/run_bench.py --tag baseline                 # all of the above, parsed
vivado -mode batch -source scripts/report_power.tcl   # power estimate
```

**From the GUI** — `a7lite_soc/a7lite_soc.xpr` is the project to open. It references
`rtl/`, `constr/`, `vendor/cv32e40x/rtl/` and `build/` **in place**, so editing a repo file
is enough and nothing needs re-importing:

```bash
./fw/build.sh gate                                    # MUST run before opening Vivado
source /opt/Xilinx/2026.1/Vivado/settings64.sh
vivado a7lite_soc/a7lite_soc.xpr
# Reset Run on synth_1 -> Run Synthesis -> Run Implementation -> Generate Bitstream
# then Hardware Manager -> program
py/venv/bin/python scripts/capture_uart.py --seconds 90 --expect '#GATE'
```

`scripts/create_gui_project.tcl` regenerates a *disposable* equivalent into `vivado_gui/`.
It is the rescue path if `a7lite_soc.xpr` is ever lost — not the normal way in.

> **The firmware is baked into the BRAM initialisation.** Any firmware change needs a full
> re-elaboration: **re-run synthesis, not just implementation.** `--no-bit` after editing a
> kernel silently measures the *old* code. This is the easiest way to produce wrong numbers.

The week-2 gate, end to end:

```bash
gcc -O2 -I fw -I py -DGOLDEN_HEADER='"golden_host.h"' \
    -o /tmp/hc py/host_check.c fw/ssm_model.c && /tmp/hc   # host, ~1 s, 240 beats
./fw/build.sh gate                                          # then synth + program
```

Model development (all CPU, no GPU needed):

```bash
py/venv/bin/python py/fetch_data.py    # MIT-BIH -> py/data/mitdb (parallel, resumable)
py/venv/bin/python py/prep_data.py     # -> beats.npz, 288-sample 3-beat windows @120 Hz

py/venv/bin/python py/test_model.py       # guards the scan against a naive reference
py/venv/bin/python py/test_fixedpoint.py  # integer primitives (no checkpoint needed)
py/venv/bin/python py/test_quantize.py    # determinism, no float leaks, no overflow

py/venv/bin/python py/train.py --epochs 20 --out py/data/model_v4.pt   # ~50 min
py/venv/bin/python py/quantize.py --ckpt py/data/model_v3.pt --n 1000  # int vs float
py/venv/bin/python py/export_c.py --ckpt py/data/model_v3.pt --beats 9 --host-beats 240
```

> **`--test` is required to read DS2.** Model selection uses the DS1 holdout and never
> needs it. Every extra DS2 read is a chance to select on the test set (§5.3).

> **Never pass `--threads` above 4.** At 8 threads training is **31× slower**
> (doc/BRINGUP.md §13).

**Layout:** `rtl/` SoC · `fw/` firmware (+`fw/lib/` io+perf) · `constr/` XDC ·
`scripts/` build/program/bench/power · `py/` model development · `a7lite_soc/` Vivado GUI
project · `blinky/` stage-1 reference · `vendor/cv32e40x` pinned at `d952cd6` (gitignored,
restore with `scripts/fetch_core.sh`) · `build/`, `results/` generated ·
`doc/BRINGUP.md` bring-up log + hard-won facts (§13) · `doc/MEASUREMENT.md` counter map ·
`doc/SESSION-2026-08-09.md` Step 3 working log.

---

## 4. Measurements (2026-08-09, `-O2`, 50 MHz)

| kernel | cycles | instret | IPC | loads | stores |
|---|---|---|---|---|---|
| `nop` | 0 | 0 | – | 0 | 0 |
| `memcpy32` | 199 | 135 | 0.678 | 33 | 1 |
| `dot_i8` | 2312 | 1801 | 0.779 | 513 | 1 |
| `gemm_i8` | 5025 | 4002 | 0.796 | 1025 | 1 |
| `ssm_scan_q15` | 11201 | 9669 | 0.863 | 2084 | 532 |

> **`ssm_scan_q15` in `fw/bench.c` is NOT a model.** Synthetic kernel, LCG-random
> coefficients, no task, no trained weights, no accuracy. It measures the *shape* of the
> computation and nothing more.

### The real model, on hardware

`fw/gate.c`, one full inference of the trained D=32/N=16 model at T=288, 2 layers.
Measured 2026-08-13 in this repo; the archive's numbers are shown for comparison
because **they differ, and the reason is instructive** (see below).

| metric | this repo | archive (2026-08-09) |
|---|---|---|
| cycles | **31,903,407** | 33,874,576 |
| instructions | 27,156,479 | 27,155,608 |
| IPC | **0.851** | 0.802 |
| wall time @ 50 MHz | **0.638 s** (1.57 inf/s) | 0.677 s (1.48 inf/s) |

> ⚠️ **The baseline is sensitive to code layout at ~6%, and that is a measurement
> hazard for this whole project.** Same source, same `-O2`, same core, same clock —
> and the instruction count is identical to within 871 (0.003%). But cycles fell
> **5.8%**. The only difference is that this repo's `fw/build.sh` compiles with
> `-ffunction-sections -fdata-sections`, which relocates every function. With RVC,
> whether a hot loop starts on a 4-byte or 2-byte boundary changes instruction-fetch
> alignment behaviour, and across 27M instructions of tight scan loops that is worth
> ~2M cycles.
>
> Both numbers are reproducible to the digit on their own build, so this is not
> noise — it is a deterministic function of layout. **The consequence: a 6% swing
> can be manufactured by a compiler flag that has nothing to do with the
> accelerator.** Any speedup claim below roughly 1.1× is inside layout noise unless
> the baseline and the accelerated build are compiled identically. Fix the flags
> before the coupling sweep and record them in every results `.meta`.

**Use 31,903,407 as the baseline**, not the archive's number: it is the faster of
the two, and quoting the slower one would inflate the measured speedup.

At 0.638 s against a beat every ~0.8 s at 75 bpm, the core is at **~80% duty cycle**
just to keep up.

**This supersedes `ssm_scan_q15` as the baseline.** Two things the proxy could not show:

- **The workload is real-time-marginal *today*** at ~80% duty cycle — a much stronger
  motivation for the accelerator than a synthetic cycle count, and a number for the writeup.
- **The real kernel is ~2.4× less efficient per scan iteration than the proxy**: 52.6
  cycles/iteration (attributing 48.6% of the time to the scan over 294,912 iterations)
  against the proxy's 21.9. `ssm_model.c` was written for *bit-exactness* and has had no
  optimisation pass at all — branchy rounding helpers, a multiply-and-shift per state
  element, and 576 LayerNorms with hardware divides.

> ⚠️ **Do not use this number as the accelerator's speedup baseline yet.** A naive software
> baseline inflates the measured speedup, and that is the first thing an examiner will
> attack. Optimise `ssm_model.c` first (keeping it bit-exact — `py/host_check.c` makes that
> a one-second check) and quote the speedup against the *optimised* baseline. The gap
> between 52.6 and 21.9 cycles is software headroom, not accelerator headroom.

**Utilisation at 128 KB:** 3724 LUTs (17.9%), 2299 FF (5.5%), **32 RAMB36 (64%)**, 3 DSP.
**WNS +2.360 ns, WHS +0.092 ns → Fmax ≈ 56.7 MHz, ~13% margin at 50 MHz.** The accelerator
must stay off the critical path or the clock comes down. Fix the target frequency
deliberately before the coupling sweep or PPA comparisons are confounded.

**Power (vectorless):** total 0.081 W, dynamic 0.009 W, static 0.072 W — **static dominates
8:1**. The board runs off USB +5 V with no current-sense shunt, so a few mW of accelerator
dynamic power will not resolve against the whole board with a ~10 mA USB meter. Use
SAIF-driven estimation as the primary metric, board current as a coarse sanity check, and
report **energy per inference**.

⚠️ **Gap: the SAIF power flow is documented but not built.** Vectorless works today; SAIF
is what a defensible PPA claim needs, and every coupling-sweep point will need one.

---

## 5. Three open questions that gate the accelerator RTL

### 5.1 There is no memory bottleneck to remove — reframe the sweep

`ld_stall` and `wb_data_stall` measure **0 on every kernel**. That is *correct*: the BRAM is
zero-wait-state and structurally cannot stall (`gnt = req`, `rvalid` next cycle).

Consequence: any speedup measured here comes **entirely** from collapsing instruction count,
and is a **lower bound** versus a realistic memory system. An examiner will notice.

**Suggested fix that turns it into a contribution:** make data-memory wait states a
parameter and run the coupling sweep at 0/1/2/4 wait states. "Crossover as a function of
memory latency" is a stronger result than a single crossover number, for ~30 lines of RTL.
Decide before building the accelerator — it changes what the sweep measures.

### 5.2 The scan is only half the work — a scan-only accelerator caps at 2×

Integer ops per inference at T=288, D=32, N=16, 2 layers:

| component | share |
|---|---|
| **SSM scan** | **48.6%** |
| **projections** (dt/B/C/out) | **48.6%** |
| LayerNorm (incl. rsqrt) | 2.8% |
| softplus LUT + pool + head | ~0.8% |

If the CV-X-IF instruction accelerates only the scan, Amdahl caps the whole-model speedup
at **2.0×** no matter how good the accelerator is.

The ratio is `6DN / (2D² + 2DN)`: projection *parameters* scale as D² while scan *work*
scales as D·N, so a narrower model with more state shifts work into the scan at constant
parameter count. D=16/N=64 gives 2.40:1 (scan 70.6%, ceiling **3.4×**) at 7,701 params —
**but it did not survive the inter-patient test, see §5.3.** Either widen what the
instruction covers (the projections are plain INT8 GEMV) or find a D:N that generalises.

*Corollary:* LayerNorm is only 2.8%, so **keep it**. The earlier worry about rsqrt
dominating was wrong — it is O(T·D) against the scan's O(T·D·N).

### 5.3 Validation did not predict inter-patient generalisation

| | D=32/N=16 | D=16/N=64 |
|---|---|---|
| val macro-F1 | 0.6162 | 0.6160 |
| **DS2 macro-F1** | **0.5596** | **0.5065** |
| V sensitivity | 80.5 | 86.5 |
| **V precision** | **59.9** | **39.5** |

Tied on validation, clearly different on DS2. **Do not choose an architecture on the DS1
holdout.** The 4-record validation split does not capture cross-patient variation.

**Both configs over-predict V, and the cause is ours:** `train.py` applied *two* imbalance
corrections at once — inverse-√frequency class weights (V ≈2.3× over N) **and** N
undersampling 38,096→12,000 (a further 3.2×). Combined, V is ~7× over-represented against
DS2's true 13.7:1 ratio.

> ⚠️ **Tested 2026-08-09 — the fix did not work, and may be wrong.** Dropping the class
> weights gave DS2 macro-F1 **0.5071** against 0.5596, with V precision *falling*
> 59.9 → 37.1. The opposite of the prediction. But a final LayerNorm was added in the same
> run, so two variables moved at once, and the 0.0525 gap is exactly the single-seed noise
> this section warns about. An isolation run is what decides it. **The effective-ratio
> calculation stands** regardless; it just is not the whole story on V precision.

A 0.05 macro-F1 gap on a single seed should not decide the accelerator architecture.
Re-run with 2–3 seeds before committing — **this was violated once already.**

---

## 6. Accuracy work — parallel to the critical path, not blocking it

- **S is the weak class: 10.1% sensitivity on DS2** (N 89.1/94.8, V 80.5/59.9). S beats are
  normal in *morphology* and identified by arriving **early**. Widening the window to
  ~3 beats took S from 0.00% to ~10%, so it is learnable, but the literature (de Chazal
  ~76%) uses explicit **RR-interval features** — pre-RR, post-RR, ratio to local average.
  Three scalar inputs that do not touch the scan, so they cost ~nothing in the accelerated
  path. **Highest-value accuracy fix.**

### Open questions for Dr Jeevan

1. **Classification or anomaly detection?** `prep_data.py` builds AAMI 5-class labels,
   which collapse to binary (N vs rest) if he wants anomaly detection — and that framing
   would largely dissolve the S problem. Worth asking before investing in RR features.
2. **The R-peak segmentation frontend.** Beat classification needs R-peak detection, and a
   QRS detector is the kind of frontend ECG was chosen over keyword spotting to avoid.
   Convention is to segment on *annotated* R-peaks and declare it, which is what
   `prep_data.py` does — but it is a soft spot in "the SSM is the workload", and he should
   hear it from you rather than from an examiner.

---

## 7. Decisions made

| Decision | Choice | Why |
|---|---|---|
| Core | CV32E40X (not Ibex) | brought up cleanly; keeps the CV-X-IF standard extension interface |
| **Step 3 task** | **MIT-BIH ECG beat classification** (AAMI 5-class) | ⚠️ classification-vs-anomaly-detection unconfirmed with Dr Jeevan (§6) |
| Reset | on-chip power-on counter, no button | manual's reset key "K3" absent from its own key table |
| RAM | four byte-wide lanes, 128 KB | Vivado drops byte write-enables above a 12-bit address (doc/BRINGUP.md §11) |
| `NUM_MHPMCOUNTERS` | 1, events multiplexed across re-runs | kernels are deterministic; only ~13% timing margin to spend |

**Why ECG over keyword spotting:** KWS needs an MFCC/filterbank frontend running on the
core. That is FFT-heavy and would likely dominate the cycle budget — you would be
accelerating the SSM while the frontend eats the gains, confounding the measurement. ECG
feeds raw samples straight into the scan, so **the SSM is the workload**. Dataset is also
~100 MB rather than ~2 GB.

---

## 8. Next actions

**In order:**

1. **Optimise `ssm_model.c`** (§4) — it is written for bit-exactness, not speed, and must
   not become the accelerator's baseline as-is. Keep `py/host_check.c` green throughout.
2. **Answer §5.1** (wait states), **§5.2** (scan vs projections) and **§5.3** (D:N) —
   all three change what gets built.
3. **Build the SAIF power flow** (xsim testbench + `write_saif`).
4. **Fix the target clock frequency** for all PPA comparisons.
5. Then start the Step 4 RTL.

**Also outstanding:** fold the real model kernel into `fw/bench.c` so the coupling sweep can
measure it alongside the synthetic kernels (`gate.c` measures it today).

---

## 9. Housekeeping

- `~/a7lite-cv32e40x` is the archive the gate was originally passed in. It is intact and
  clean on branch `step3-ssm-on-core`. **Do not resume work there** — this repo is ahead.
- Old attempts under `~/projects/`, `~/vivado_projects/`, `~/parent/`, `~/cv32e40x*`,
  `~/a7lite_soc` were abandoned mid-debug and contain broken RTL. **Do not restart from
  them.**
- `py/venv/` (5.0 GB) and `py/data/` (138 MB) are gitignored and were copied, not
  re-bootstrapped. The checkpoints in `py/data/*.pt` are **not reproducible from this
  repo** — back them up somewhere outside git.
