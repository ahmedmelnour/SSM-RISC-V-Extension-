# Reproduction prompt — the selective SSM for MIT-BIH beat classification

Paste everything below the line into a fresh session (or hand it to a collaborator) to
rebuild `py/model.py`, `py/prep_data.py` and `py/train.py` from scratch. Every design
decision carries the reference it came from, and the deliberate deviations from Mamba are
marked as such.

Acceptance criterion: **exactly 7,781 parameters**, and the exported fixed-point model
passes `fw/gate.c` at 9/9 exact against the float reference.

---

## Task

Build a **selective state-space model** for single-lead ECG heartbeat classification, in
PyTorch, targeting deployment on a RISC-V microcontroller with **no FPU and 128 KB of RAM**
(OpenHW CV32E40X, RV32IMC_Zicsr). Then quantise it to integer arithmetic and export it to C.

The model is not a general-purpose Mamba block. It is deliberately reduced, and each
reduction below is a hardware constraint, not a modelling preference.

## Three constraints that determine everything

1. **≤ 10k parameters, INT8 weights** — code, weights and stack share one 128 KB BRAM.
2. **The inner loop is what a custom instruction will accelerate** — so the reference
   implementation must read the way the hardware works: a sequential scan, one
   multiply-accumulate against a decay term and one against an input term, per timestep per
   state.
3. **Every operation must have an exact fixed-point form** — no `exp`, no division, no
   square root inside the recurrence.

## Data pipeline

- **Dataset:** MIT-BIH Arrhythmia Database [1], via PhysioNet [2] (`wfdb` package).
- **Channel:** select **MLII by name, not by index** — it is channel 0 in most records but
  channel 1 in record 114.
- **Excluded records:** 102, 104, 107, 217 (paced). Required by **ANSI/AAMI EC57** [3].
- **Labels:** group MIT-BIH annotation symbols into the **5 AAMI EC57 classes**
  N, S, V, F, Q [3].
- **Windowing:** 360 Hz native, decimate by 3 → **120 Hz**; window = **288 samples**
  (2.4 s, ≈3 beats), centred on the annotated R-peak.
- **Split:** the **inter-patient DS1/DS2 protocol** of de Chazal et al. [4] — train on DS1,
  test on DS2, never mix records. Hold out four DS1 records for epoch selection, chosen to
  carry a usable number of S and V beats. *A beat-wise split leaks the patient into model
  selection and gives a number that does not survive DS2.*
- Report **per-class sensitivity**, not accuracy — the N class dominates heavily.

## Architecture

```
ECGNet(d_model=32, d_state=16, n_layers=2, n_classes=5)

  in_proj : Linear(1, 32)                    # one scalar sample per timestep -> 32 channels
  for each of 2 layers:
      LayerNorm(32) -> SelectiveSSM(32, 16) -> residual add
  mean-pool over time
  head    : Linear(32, 5)
```

### `SelectiveSSM(d_model=32, d_state=16)`

Parameters:

| name | shape | note |
|---|---|---|
| `A_raw` | `(32, 16)` | init `linspace(-2.0, 1.0, 16).repeat(32, 1)`; used as `A = softplus(A_raw)` so **A > 0** — a negative A diverges and nothing downstream catches it |
| `D_skip` | `(32,)` | init ones — the skip term of [5] |
| `dt_proj` | `Linear(32, 32)` | **bias init −4.0**, so Δ starts small (see discretisation) |
| `B_proj` | `Linear(32, 16)` | |
| `C_proj` | `Linear(32, 16)` | |
| `out_proj` | `Linear(32, 32)` | |

Forward, for input `x : [B, T, 32]`:

```
A   = softplus(A_raw)                       # [32, 16], > 0
dt  = softplus(dt_proj(x))                  # [B, T, 32], > 0   -- selective
Bt  = B_proj(x)                             # [B, T, 16]        -- selective
Ct  = C_proj(x)                             # [B, T, 16]        -- selective

h = zeros(B, 32, 16)
for t in 0..T-1:
    a = (1 - dt[t].unsqueeze(-1) * A).clamp(0.0, 0.999)     # [B, 32, 16]
    b = (dt[t] * x[t]).unsqueeze(-1) * Bt[t].unsqueeze(1)   # [B, 32, 16]
    h = a * h + b
    y[t] = (h * Ct[t].unsqueeze(1)).sum(-1) + D_skip * x[t]
return out_proj(stack(y, dim=1))
```

**Selectivity** — Δ, B and C are projections of the input and therefore change every
timestep. This is the defining property of Mamba/S6 [5] as opposed to the time-invariant
S4 [6] and S5, and it is what prevents hoisting the coefficients out of the loop. `A` is
**diagonal per (channel, state)**, which is Mamba-1's structure — *not* Mamba-2's
scalar-times-identity per head [7].

**Implementation note:** call `.unbind(1)` once on `dt`, `B`, `C` and `x` before the loop
rather than indexing `[:, t]` inside it. They are equivalent forward, but indexing a
`[B,T,D,N]` tensor T times makes backward allocate and zero a full-size gradient buffer per
step — at batch 256 that is 67 MB × 128 steps, and made backward 14× the cost of forward.

## The one deliberate deviation from Mamba: Euler discretisation

Mamba uses `ā = exp(Δ·A)` [5]. **This model uses forward Euler instead:**

```
a = 1 - Δ·A,   A > 0,   clamped to [0, 0.999]
b = Δ·x·B                              # no φ₁ term
```

Rationale: exact `exp` in fixed point needs a lookup table and argument reduction, on a core
with no FPU, **inside the loop being accelerated**. Euler is one multiply and one subtract.
It is the first-order approximation of `exp(−Δ·A)`, stable exactly when `Δ·A < 1`, which the
clamp enforces. The `dt_proj` bias init of −4.0 keeps Δ small enough that the approximation
holds — a large init walks into the clamp, where the gradient is zero and the block cannot
learn.

Position this precisely against the discretisation taxonomy of Mamba-3 [8, §3.1, Table 1]:

| | `ā` | `b̄` | order |
|---|---|---|---|
| **this model** | `1 − ΔA` | `Δ·B` | forward Euler |
| Mamba-1/2 released code | `exp(ΔA)` | `Δ·B` | exponential-Euler [8] |
| FEMBA [9] | `exp(ΔA)` | `Δ·φ₁(ΔA)·B` | exact ZOH |
| Mamba-3 [8] | `exp(ΔA)` | 3-term, `λ_t` data-dependent | trapezoidal, O(Δ³) |

> ⚠️ **This substitution changes the model.** Any accuracy comparison must be against *this*
> model trained this way — never against a Mamba baseline that used `exp`. State the
> substitution as a claim in the write-up, and report float-vs-fixed agreement so the cost is
> measured rather than assumed.

## Do NOT add

Each of these is a considered omission, not an oversight:

- **No causal depthwise conv1d.** Mamba's short conv [5] is a separate kernel with its own
  state; it does not fit the parameter budget and is not what the instruction accelerates.
  (Mamba-3 [8] argues the trapezoidal rule makes it redundant anyway.)
- **No gating branch.** No `in_proj → 2·d_inner`, no `out = SSM(x) ⊙ SiLU(z)`. SiLU needs a
  second nonlinearity in the datapath.
- **No parallel or associative scan.** The sequential loop is the point — it is the exact
  computation the hardware replaces, so the reference should read the way the hardware works.
- **No `exp` anywhere in the recurrence.** See above.
- **No RMSNorm.** LayerNorm has an exact fixed-point form already implemented
  (`py/fixedpoint.py`); swapping norms means re-deriving it.

## Training

- 15 epochs, batch 256, lr 3e-3, Adam.
- **Class-weighted loss** plus a cap on N beats in training only — the class imbalance is
  severe. The F class has so few examples that it should be expected to be predicted
  essentially never; say so rather than hiding it.
- Select the epoch on the held-out DS1 records, never on DS2.

## Quantisation and export

- **INT8 weights, Q15 state**, per-tensor activation scales and per-channel weight scales.
- All rounding **round-to-nearest, ties away from zero**; floor division must match numpy's
  `//`, not C's truncation.
- The C port must reproduce the Python quantised reference **bit-exactly** — export a golden
  reference alongside the weights and gate on it.
- 16 bits of state is a floor, not a choice: at a static scale, 12 bits costs 7.6× perplexity
  on Mamba-130m and 8 bits costs 153× (measured; consistent with Quamba [10] and FEMBA [9],
  both of which keep the recurrence high-precision).

---

## References

**Verified against the project vault (`~/Documents/Mamba/papers/`):**

[5] A. Gu and T. Dao, "Mamba: Linear-Time Sequence Modeling with Selective State Spaces,"
    COLM 2024; arXiv:2312.00752v2, 2024.

[6] A. Gu, K. Goel, and C. Ré, "Efficiently Modeling Long Sequences with Structured State
    Spaces," ICLR 2022; arXiv:2111.00396v3, 2022.

[7] T. Dao and A. Gu, "Transformers are SSMs: Generalized Models and Efficient Algorithms
    Through Structured State Space Duality," ICML 2024; arXiv:2405.21060v1, 2024.

[8] A. Lahoti, K. Y. Li, B. Chen, C. Wang, A. Bick, J. Z. Kolter, T. Dao, and A. Gu,
    "Mamba-3: Improved Sequence Modeling using State Space Principles,"
    arXiv:2603.15569v1, 2026. (Preprint — hold the numbers loosely.)

[9] A. Tegon, N. Lehmann, Y. Li, A. Cossettini, L. Benini, and T. M. Ingolfsson, "FEMBA on
    the Edge: Physiologically-Aware Pre-Training, Quantization, and Deployment of a
    Bidirectional Mamba EEG Foundation Model on an Ultra-low Power Microcontroller,"
    arXiv:2603.26716v1, 2026.

[10] H.-Y. Chiang, C.-C. Chang, N. Frumkin, K.-C. Wu, and D. Marculescu, "Quamba: A
    Post-Training Quantization Recipe for Selective State Space Models,"
    arXiv:2410.13229v2, 2024.

**Standard references — cited from general knowledge, VERIFY before submission:**

[1] G. B. Moody and R. G. Mark, "The impact of the MIT-BIH Arrhythmia Database,"
    IEEE Engineering in Medicine and Biology Magazine, vol. 20, no. 3, pp. 45–50, 2001.

[2] A. L. Goldberger et al., "PhysioBank, PhysioToolkit, and PhysioNet," Circulation,
    vol. 101, no. 23, pp. e215–e220, 2000.

[3] ANSI/AAMI EC57, "Testing and Reporting Performance Results of Cardiac Rhythm and ST
    Segment Measurement Algorithms," Association for the Advancement of Medical
    Instrumentation.

[4] P. de Chazal, M. O'Dwyer, and R. B. Reilly, "Automatic classification of heartbeats using
    ECG morphology and heartbeat interval features," IEEE Transactions on Biomedical
    Engineering, vol. 51, no. 7, pp. 1196–1206, 2004.

[11] OpenHW Group, "CV32E40X User Manual" and "CV-X-IF: eXtension Interface specification."
