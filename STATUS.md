# Project status & handoff

**Read this first in a new session.** It carries the context needed to continue without
re-deriving anything. Last updated **2026-08-09** (~day 2 of a 10-week FYP).

```
Repo:      ~/a7lite-cv32e40x    git, 6 commits, WORKING TREE DIRTY - nothing from
                                2026-08-09 is committed yet (see below)
Board:     MicroPhase A7-Lite, Artix-7 xc7a35tfgg484-2L, connected & working
Vivado:    /opt/Xilinx/2026.1   BASIC license, 289 parts.
                                `source /opt/Xilinx/2026.1/Vivado/settings64.sh` first --
                                vivado is NOT on PATH (§5)
Toolchain: ~/tools/xpack-riscv-none-elf-gcc-13.2.0-2/bin/riscv-none-elf-gcc
Python:    py/venv/bin/python   torch 2.13, wfdb, sklearn (bootstrap notes: py/README.md)
```

**Uncommitted work from 2026-08-09.** Modified: `rtl/a7lite_soc_top.sv`, `fw/link.ld`,
`fw/build.sh`, `fw/main.c`, `README.md`, `STATUS.md`, `.gitignore`. New: all of `py/`,
`doc/SESSION-2026-08-09.md`. Suggested split: (1) the 128 KB widening + its hardware
verification, (2) the Python model-development tree, (3) the quantization rework
(`py/fixedpoint.py`, `py/quantize.py`, `py/test_fixedpoint.py`, `py/test_quantize.py`).
`clockInfo.txt` also shows as modified — it is a Vivado byproduct that re-dirties on every
build and describes `design_1`, not this design; consider `git rm --cached` + gitignore.

**Checkpoints in `py/data/` (gitignored).** `model_d32n16_run3.pt` is run #3, DS2 0.5596 —
**still the best model**, and the one to port if the isolation run does not beat it.
`model_v2.pt` is the two-variable run (DS2 0.5071, has the reverted final LayerNorm, so
`quantize.py` reconstructs it). `model_v3.pt` is the isolation run. `model_d16n64.pt` is
the §7.3 architecture comparison. Note `model.pt` was the run-3 file and is superseded by
the explicit name.

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
identical results at `-O0/-Os/-O2/-O3`, clean under UBSan — before anything was
synthesised.

---

## 2. Where we are

| Step | Budget | Status |
|---|---|---|
| **1. Core bring-up** | days 1–3 | ✅ **complete** |
| **2. Measurement infra** | days 4–5 | ✅ **complete**, one gap (SAIF) |
| **3. SSM in software** | days 6–12 | ✅ **complete — gate passed 2026-08-09** (§8) |

**The week-2 gate is passed** (§1) — the trained, quantized SSM runs on the CV32E40X and
matches the Python reference bit for bit. Steps 1–3 are complete with ~12 days of budget
left over. See `doc/SESSION-2026-08-09.md` for the full working log.

**The critical path is now Step 4, the CV-X-IF instruction.** Three §7 questions gate the
RTL and none of them is answered yet (§7.2 wait states, §7.3 scan share, §7.4 D:N).

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

### Step 3 ✅ complete

> **`ssm_scan_q15` in `fw/bench.c` is NOT a model.** It is a synthetic benchmark kernel
> with LCG-random coefficients — no task, no trained weights, no accuracy, no Python
> reference. It exists to measure the *shape* of the computation, and it did that job.
> Do not count it as Step 3 progress.

**Done:** MIT-BIH fetched and preprocessed (`py/fetch_data.py`, `py/prep_data.py`);
selective SSM trained (`py/model.py`, `py/train.py`), 7,781 params, **DS2 inter-patient
macro-F1 0.5596, V sensitivity 80.5%**; fixed-point reference built, validated and
guarded (`py/fixedpoint.py`, `py/quantize.py`, `py/test_fixedpoint.py`,
`py/test_quantize.py`).

**The quantization blocker is cleared.** The integer reference agreed with the float model
only ~62% of the time; it is now **97%**, with zero saturations and zero LayerNorm-gain
clips. That took per-tensor activation scales *and* per-channel weight scales *and* —
mostly — fixing three hardcoded shift constants (§5). Note ">99%" is not reachable while
weights stay INT8; ~98.9% is the ceiling.

**Ported and passing on hardware.** `py/export_c.py` → `fw/model_data.h` + `fw/golden.h`;
`fw/ssm_model.c` transcribes `quantize.py`; `fw/gate.c` compares on-board. 9/9 bit-exact,
first real-model measurement in §6 (33.9M cycles, 0.677 s/inference, ~85% duty cycle).

**Two findings that change what gets built — read §7.3 and §7.4.**

---

## 3. Decisions made

| Decision | Choice | Why |
|---|---|---|
| Core | CV32E40X (not Ibex) | brought up cleanly; keeps the CV-X-IF standard extension interface |
| **Step 3 task** | **MIT-BIH ECG beat classification** (AAMI 5-class) | see below; ⚠️ classification-vs-anomaly-detection still unconfirmed with Dr Jeevan (§8) |
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

# the week-2 correctness gate, end to end
py/venv/bin/python py/export_c.py --ckpt py/data/model_v3.pt --beats 9 --host-beats 240
gcc -O2 -I fw -I py -DGOLDEN_HEADER='"golden_host.h"' \
    -o /tmp/hc py/host_check.c fw/ssm_model.c && /tmp/hc   # host, ~1 s, 240 beats
./fw/build.sh gate                                    # then synth + program as above
py/venv/bin/python scripts/capture_uart.py --port /dev/ttyUSB1 --seconds 90 --expect '#GATE'

vivado -mode batch -source scripts/create_gui_project.tcl   # makes vivado_gui/a7lite_soc.xpr
```

> **The firmware is baked into the BRAM initialisation.** Any firmware change needs a full
> Vivado rebuild. `--no-bit` after editing a kernel silently measures the *old* code. This
> is the easiest way to produce wrong numbers.

Model development (all CPU, no GPU needed):

```bash
py/venv/bin/python py/fetch_data.py    # MIT-BIH -> py/data/mitdb (parallel, resumable)
py/venv/bin/python py/prep_data.py     # -> beats.npz, 288-sample 3-beat windows @120 Hz

py/venv/bin/python py/test_model.py       # guards the scan against a naive reference
py/venv/bin/python py/test_fixedpoint.py  # integer primitives (no checkpoint needed)
py/venv/bin/python py/test_quantize.py    # determinism, no float leaks, no overflow

py/venv/bin/python py/train.py --epochs 20 --out py/data/model_v3.pt   # ~50 min
py/venv/bin/python py/train.py --epochs 20 --test                      # ...and read DS2

py/venv/bin/python py/quantize.py --ckpt py/data/model_v3.pt --n 1000  # int vs float
py/venv/bin/python py/quantize.py --global-fa 12          # reproduce the old blocker
py/venv/bin/python py/quantize.py --per-tensor-weights    # ablate per-channel scales
```

> **`--test` is required to read DS2.** Model selection uses the DS1 holdout and never
> needs it. Every extra DS2 read is a chance to select on the test set (§7.4).

> **Never pass `--threads` above 4.** At 8 threads training is **31× slower** while
> pegging every core (§5). A run left at `os.cpu_count()` took five hours to reach
> epoch 2 of 20.

**Layout:** `rtl/` SoC · `fw/` firmware (+`fw/lib/` io+perf) · `constr/` XDC ·
`scripts/` build/program/bench/power · `py/` model development (fetch, prep, model,
train, quantize, fixedpoint, tests — see `py/README.md`; `py/venv/` and `py/data/`
gitignored) · `vendor/cv32e40x` upstream pinned at `d952cd6` (gitignored, restore with
`scripts/fetch_core.sh`) · `build/`, `results/`, `vivado_gui/` generated ·
`doc/BRINGUP.md` full walkthrough incl. GUI equivalents · `doc/MEASUREMENT.md` counter
map and power methodology · `doc/SESSION-2026-08-09.md` working log for Step 3.

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

**Indexing `t` out of a `[B,T,D,N]` tensor inside a scan makes backward 14x forward.**
The first version of `SelectiveSSM.forward` precomputed `a` for all timesteps and then used
`a[:, t]` in the loop. Forward was 0.74 s, backward 10.56 s — because each `select` backward
allocates and zeros a gradient buffer the size of the *whole* tensor (67 MB at batch 256),
128 times per layer. Calling `.unbind(1)` once instead lowers the whole thing to a single
stack in backward: 0.42 s, and an epoch went from ~1825 s to ~116 s. If the scan ever feels
inexplicably slow again, look for a `[:, t]` on a large tensor before looking anywhere else.

**`/dev/ttyUSB0` is not necessarily the UART.** The board presents *two* USB serial
devices: an **FT232H** (`0403:6014`) for JTAG and a **CH340** (`1a86:7523`) for the UART.
Which one gets `ttyUSB0` depends on enumeration order, so it changes between boots and
between cable orders. Capturing on the FT232H gives `RESULT: SILENT -- no bytes received`,
which looks exactly like dead firmware, a wrong baud, or a bad pin constraint — and costs
a full reprogram cycle to rule out, because the firmware prints once at reset and there is
no reset button (§3).

Resolve it by vendor ID rather than trusting the number:

```bash
for d in /sys/bus/usb-serial/devices/*; do
    n=$(basename "$d")
    udevadm info -q property -n /dev/$n | grep -q 1a86 && echo "UART is /dev/$n"
done
```

**`vivado` is not on `PATH` in a fresh shell.** The commands in §4 need
`source /opt/Xilinx/2026.1/Vivado/settings64.sh` first, or they die with
`vivado: command not found` — and if you redirect the log, that failure looks exactly like a
build that ran and produced nothing, while a *stale* `build/a7lite_soc.bit` from the previous
run sits there ready to be programmed. `scripts/run_bench.py` is immune: it invokes Vivado by
absolute path.

**Calibrate overhead through the exact path you measure through**, and per-event. Getting
this wrong biased every event count by exactly +20 instructions. The sweep measures
`instret` twice by independent means specifically so that class of bug shows up as a
disagreement — keep that redundancy.

**A hardcoded shift constant sized for a worst case that never occurs is invisible until
the scales around it get tight — then it dominates.** Three of them in `quantize.py`
(`SV=5` in the LayerNorm variance, an unscaled `isqrt(var)`, `SP=6` in the scan) were
harmless under a single global activation scale and became the *largest* error source the
moment scales went per-tensor: implementing per-tensor scales first made agreement go
**down**, 62% → 53%. Fixing the three constants took it to **97%** — worth far more than
every activation scale put together. All three are now derived from `D`, `N` and the int32
bound, so they follow the architecture (at N=64, `SP` must be 5; the old 6 was nearly
right by luck). When a quantization change makes things worse, suspect the fixed shifts
before the scales.

**Cheap in operations is not the same as cheap in bits.** A final LayerNorm before the
head is ~0.01% of the op budget and never touches the scan — and it grew the residual
stream **4.4×** (50.7 → 221.4), because the head reading the pooled vector *directly* is
the only thing making weight decay pay for an unbounded stream. Agreement fell 97.3% →
96.4% and per-channel weight scales stopped helping at all. Reverted; see `model.py`.
The §7.3 op-budget table is the right tool for deciding what to *accelerate* and the wrong
tool for deciding what is safe to *add*.

**If saturation goes up when you add range, stop adding range.** Saturation counts are
usually a symptom of upstream precision loss inflating values, not a symptom of the range
being too small. `--headroom 1` pushed `y` saturation from 32% to 41%; the real fault was
the LayerNorm variance shift. After fixing that, saturations went 3.3M → **0** with no
headroom at all.

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

### The real model, measured on hardware (2026-08-09, `-O2`, 50 MHz)

`fw/gate.c`, one full inference of the trained D=32/N=16 model at T=288, 2 layers:

| metric | value |
|---|---|
| cycles | **33,874,576** |
| instructions | 27,155,608 |
| IPC | 0.802 |
| wall time @ 50 MHz | **0.677 s** (1.48 inferences/s) |

**This supersedes `ssm_scan_q15` as the baseline.** Two things it reveals that the proxy
could not:

- **A beat arrives every ~0.8 s at 75 bpm, so the core is at ~85% duty cycle just to keep
  up.** The workload is real-time-marginal *today*, which is a much stronger motivation
  for the accelerator than a synthetic cycle count, and it is a number to put in the
  writeup.
- **The real kernel is ~2.5× less efficient per scan iteration than the proxy**: 55.8
  cycles/iteration (attributing 48.6% of the time to the scan, §7.3) against the proxy's
  21.9. `ssm_scan_q15` was hand-written for the measurement; `ssm_model.c` was written for
  *bit-exactness* and has had no optimisation pass at all — branchy rounding helpers, a
  multiply-and-shift per state element, and 576 LayerNorms with hardware divides.

> ⚠️ **Do not use this number as the accelerator's speedup baseline yet.** A naive
> software baseline inflates the measured speedup, and that is the first thing an examiner
> will attack. Optimise `ssm_model.c` first (keeping it bit-exact — `py/host_check.c`
> makes that a one-second check), and quote the speedup against the *optimised* baseline.
> The gap between 55.8 and 21.9 cycles is software headroom, not accelerator headroom.

**Utilisation / timing** (32 KB build, superseded — see below): 3733 LUTs (18%), 2304 FF
(5.5%), 8 RAMB36 (16%), 3 DSP, WNS +1.660 ns.

**Current, at 128 KB (2026-08-09):** 3724 LUTs (17.9%), 2299 FF (5.5%), **32 RAMB36 (64%)**,
3 DSP. **WNS +2.360 ns, WHS +0.092 ns → Fmax ≈ 56.7 MHz, ~13% margin at 50 MHz.**

Widening the memory did *not* cost timing — it gained 0.7 ns, and LUTs went slightly down.
The critical path was never the memory address decode. Note this is placement-dependent
noise as much as a real improvement; do not treat 56.7 MHz as a hard ceiling either way.
The accelerator must still stay off the critical path or the clock comes down. Fix the
frequency deliberately before the coupling sweep, or PPA comparisons are confounded.

**Power (vectorless):** total 0.081 W, dynamic 0.009 W, static 0.072 W — **static dominates
8:1**. The board runs entirely off USB +5 V with no current-sense shunt, so a few mW of
accelerator dynamic power will not be resolvable with a ~10 mA USB meter against the whole
board (DDR3, Ethernet PHY, regulators). Use SAIF-driven estimation as the primary metric,
board current as a coarse sanity check, and report **energy per inference**.

---

## 7. Two platform issues to resolve early

### 7.1 ✅ RESOLVED 2026-08-09 — RAM widened 32 KB → 128 KB

The SoC had **32 KB total for code + data + stack**, against a plan targeting "tens of
thousands of parameters" (10k INT8 params = 9.8 KB, 20k = 19.5 KB, 50k = 48.8 KB) with
`bench.c` alone taking 6.7 KB. 20k was at the ceiling; 50k did not fit.

**Done:** `MEM_WORDS` 8192 → 32768 in `rtl/a7lite_soc_top.sv`, `link.ld` `LENGTH` → 128K,
`MEM_WORDS` in `fw/build.sh` → 32768. The word index is now derived (`IDX_W`/`AW_HI`) from
`MEM_WORDS` instead of being a hardcoded `[12:0]`, so the array and the slice that addresses
it cannot drift apart — that exact mismatch is what silently broke the abandoned SoC in
`doc/BRINGUP.md` §10.2.

**Verified three ways, not just synthesised:**
- 32 RAMB36 exactly (64% of 50), as predicted. LUTs 3724, *down* 9 from the 32 KB build.
- WNS **+2.360 ns**, better than the 1.660 ns baseline. No timing cost at all.
- On hardware: all five kernels reproduce the 32 KB baseline **bit-identically** — same
  cycles, instret, loads and stores to the digit (`results/mem128k_*`). The widening is
  functionally invisible to the measurement, which is what makes the old §6 numbers still
  comparable against everything measured from here on.

**Budget now:** 128 KB for code + data + stack, 18 RAMB36 spare for the accelerator.

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

### 7.3 ⚠️ The scan is only half the work — a scan-only accelerator caps at 2×

Counting integer ops per inference at T=288, D=32, N=16, 2 layers:

| component | share |
|---|---|
| **SSM scan** | **48.6%** |
| **projections** (dt/B/C/out) | **48.6%** |
| LayerNorm (incl. rsqrt) | 2.8% |
| softplus LUT + pool + head | ~0.8% |

`ssm_scan_q15` never showed this, because it models the scan alone with no projections —
so §6's "21.9 cycles per inner iteration of headroom" describes **half the problem**.
If the CV-X-IF instruction accelerates only the scan, Amdahl caps the whole-model
speedup at **2.0×** no matter how good the accelerator is.

The ratio is `6DN / (2D² + 2DN)`: projection *parameters* scale as D² while scan *work*
scales as D·N, so a narrower model with more state shifts work into the scan at constant
parameter count. D=16/N=64 gives 2.40:1 (scan 70.6%, ceiling **3.4×**) at 7,701 params.

**But it did not survive the inter-patient test — see §7.4.** Either widen what the
instruction covers (include the projections, which are plain INT8 GEMV), or find a D:N
that generalises. Decide **before** the accelerator RTL.

*Corollary:* LayerNorm is only 2.8%, so **keep it**. Earlier worry about rsqrt dominating
was wrong — it is O(T·D) against the scan's O(T·D·N).

### 7.4 ⚠️ Validation did not predict inter-patient generalisation

Two configs, same parameter budget, trained identically:

| | D=32/N=16 | D=16/N=64 |
|---|---|---|
| val macro-F1 | 0.6162 | 0.6160 |
| **DS2 macro-F1** | **0.5596** | **0.5065** |
| V sensitivity | 80.5 | 86.5 |
| **V precision** | **59.9** | **39.5** |

Tied on validation, clearly different on DS2. **Do not choose an architecture on the DS1
holdout.** The 4-record validation split does not capture cross-patient variation; only
DS2 does, and DS2 must stay read-once.

**Both configs over-predict V, and the cause is ours:** the training script applies
*two* imbalance corrections at once — inverse-√frequency class weights (V ≈2.3× over N)
**and** N undersampling 38,096→12,000 (a further 3.2×). Combined, V is ~7× over-
represented relative to DS2's true 13.7:1 N:V ratio, which shows up exactly as
minority-class false positives. **Use one correction, not both.**

> ⚠️ **Tested 2026-08-09 — the fix did not work, and may be wrong.** Dropping the class
> weights (keeping the N cap, which the effective-ratio table in `train.py` shows is the
> better of the two) gave DS2 macro-F1 **0.5071** against 0.5596, with V precision
> *falling* 59.9 → 37.1 — 1,612 N→V false positives became 4,773. The opposite of the
> prediction above.
>
> Do not treat that as settled either: a final LayerNorm was added in the same run, so
> two variables moved at once, and the 0.0525 gap is exactly the single-seed noise this
> section already warns about. An isolation run (imbalance change only) is what decides
> it. Whatever the answer, **the effective-ratio calculation stands** — the old recipe
> really was over-representing V ~6× and S ~4.8×; it just is not the whole story on
> V precision.

Also: a 0.05 macro-F1 gap on a single seed should not decide the accelerator
architecture. Re-run with 2–3 seeds before committing. **This applies to the imbalance
question above too** — it was violated once already.

---

## 8. Next actions — Step 3, MIT-BIH ECG

**4 of 10 done.** Items 1–4 complete, 5–7 blocked on one issue, 8–9 untouched.

| # | Item | State |
|---|---|---|
| 1 | Widen SoC to 128 KB | ✅ §7.1 |
| 2 | Python environment | ✅ `py/README.md` |
| 3 | Fetch MIT-BIH | ✅ 48/48 records |
| 4 | Train ≤10k-param SSM | ✅ DS2 macro-F1 0.5596 (run #3, still the best) |
| 5 | Quantize INT8 / INT16 state | ✅ per-tensor act + per-channel weight scales |
| 6 | Validate float vs quantized | ✅ **97%**, 0 saturations, 0 LayerNorm clips |
| 7 | Fixed-point reference | ✅ + `test_fixedpoint.py`, `test_quantize.py` |
| 8 | Export to C, port, bare-metal | ✅ `py/export_c.py`, `fw/ssm_model.c`, `fw/gate.c` |
| 9 | Add real kernel to `fw/bench.c` | 🔄 `gate.c` measures it; fold into bench for the sweep |
| 10 | **Gate check** | ✅ **PASSED 2026-08-09, 9/9 bit-exact** |

**9.5 of 10, twelve days early.** Items 5–7 were unblocked mostly by fixing three
hardcoded shift constants, not by the scales themselves (§5).

**Step 3 is done. The critical path is now Step 4: the CV-X-IF instruction itself.**
Before starting the RTL, three questions in §7 are still open and all three change what
gets built: the wait-state decision (§7.2), the scan-vs-projection split (§7.3), and the
D:N ratio (§7.4). Also optimise `ssm_model.c` before quoting any speedup (§6).

### Do these in order

**A. Retrain — ⚠️ partially done, one question still open.**
   1. **Drop one imbalance correction** — done. `train.py` now defaults to
      `--class-weights none` and keeps the N cap; the effective-ratio table justifying
      that choice is a comment in the file. **But the hypothesis is NOT confirmed:** the
      first run scored DS2 0.5071 against run #3's 0.5596, with V precision *falling*
      59.9 → 37.1. Two variables changed at once, and the 0.0525 gap is the same size
      §7.4 already called single-seed noise. A clean isolation run is in progress.
   2. ~~**Bound activations during training.**~~ **Measured and rejected.** The
      activations are large because the state integrates over ~100–130 timesteps —
      structural to an SSM, not a training pathology. Lowering the `a` clamp leaves the
      widest tensor (`y`) untouched and collapses val macro-F1 0.564 → 0.165. A final
      LayerNorm made it actively *worse* (residual stream 4.4× larger — see §5). Dynamic
      range belongs in the quantizer, not the model.

**B. Quantization scales — ✅ done, but the target was wrong.** `py/quantize.py` now
calibrates a power-of-two scale per activation site *and* per weight output-channel.
Agreement went **62% → 97%**. Note the plan's ">99%" is **not reachable with INT8
weights**: per-channel INT8 with *exact* activations measures 98.9%, so ~99% is the hard
ceiling and the remaining gap is weight precision, not scales. Going past it means INT16
weights for some layers, which contradicts the stated INT8 claim — **decide before the C
port**, since the header layout depends on it.

**C. Port to C — ✅ done and passing.** `py/export_c.py` writes `fw/model_data.h`
(weights, per-channel shifts, activation scales, softplus LUT) and `fw/golden.h` (beats +
the exact logits `quantize.py` produces). `fw/ssm_model.c` is a transcription of
`quantize.py`; `fw/gate.c` runs the golden beats on the core, compares on-board and prints
`#GATE,PASS`/`FAIL` so no judgement happens over UART.

Verify without a board or a synthesis run — this is the loop to use for any change:

```bash
gcc -O2 -I fw -I py -DGOLDEN_HEADER='"golden_host.h"' \
    -o /tmp/hc py/host_check.c fw/ssm_model.c && /tmp/hc     # 240 beats, ~1 s
```

**Next: optimise `ssm_model.c`** (§6) — it is written for bit-exactness, not speed, and
must not become the accelerator's baseline as-is. Keep `host_check` green throughout.

> **Reading DS2 is now opt-in.** `train.py` needs `--test`; it used to report DS2 at the
> end of every run, which is how a "read once" test set quietly becomes a selection set.

**D. Gate check (~21 Aug):** correct on hardware? If not, switch to INT8 GEMM
immediately. Note the gate is **correctness, not accuracy** — the S-class weakness below
does not block it.

### Accuracy work — parallel to the critical path, not blocking it

- **S is the weak class: 10.1% sensitivity on DS2** (N 89.1/94.8, V 80.5/59.9).
  S beats are normal in *morphology* and identified by arriving **early**. Widening the
  window to ~3 beats (§4.3 of the session log) took S from a flat 0.00% to ~10%, so it
  is learnable, but the literature (de Chazal ~76%) uses explicit **RR-interval features**
  — pre-RR, post-RR, ratio to local average. Three scalar inputs, they do not touch the
  scan, so they cost ~nothing in the accelerated path. **Highest-value accuracy fix.**
- **A rare class can stay flat until the LR anneals.** S read as pure noise through
  epoch 14, then jumped 12× at epoch 15. Do not conclude a class is unlearnable before
  the schedule finishes — this applies to the coupling sweep too.

### Open questions for Dr Jeevan

1. **Classification or anomaly detection?** `prep_data.py` builds AAMI 5-class labels,
   which collapse to binary (N vs rest) if he wants anomaly detection — and that framing
   would largely dissolve the S problem, since S folds into "abnormal" alongside the V
   beats already detected well. Worth asking before investing in RR features.
2. **The R-peak segmentation frontend.** Beat classification needs R-peak detection, and
   a QRS detector is the kind of frontend ECG was chosen over keyword spotting to avoid
   (§3). Convention is to segment on *annotated* R-peaks and declare it, which is what
   `prep_data.py` does — but it is a soft spot in "the SSM is the workload", and he
   should hear it from you rather than from an examiner.

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
