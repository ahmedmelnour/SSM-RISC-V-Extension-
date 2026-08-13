#!/usr/bin/env python3
"""
model.py -- the selective SSM used for MIT-BIH beat classification.

The architecture is constrained from three directions at once, and every choice
below is one of those constraints rather than a modelling preference:

1. **It must fit in 128 KB with code and stack.** Target <=10k parameters, INT8
   weights (STATUS.md 7.1).
2. **Its inner loop must be the thing the CV-X-IF instruction accelerates.**
   The scan below is deliberately the same shape as `ssm_scan_q15` in
   `fw/bench.c`: per timestep, per state, one multiply-accumulate against a
   decay and one against an input term. That synthetic kernel measured 21.9
   cycles per inner iteration for ~8 ops of real arithmetic; this is the model
   that has to inherit that headroom.
3. **Every operation must have an exact fixed-point form.** See below.

## Selective, not time-invariant

B, C and the timestep dt are projections of the input, so they change every
timestep -- this is the "selective" in the project title, and it is what makes
the scan interesting to accelerate (the coefficients cannot be hoisted out of
the loop). `ssm_scan_q15` already models this: it indexes `ssm_b[t][n]` and
`ssm_c[t][n]` per timestep rather than holding them constant.

## Euler discretisation, not exp()

Mamba uses a = exp(dt * A). Exact exp() in fixed point needs a lookup table and
an argument reduction, on a core with no FPU, inside the loop being accelerated.
Instead:

    a = 1 - dt * A,   A > 0,   clamped to [0, 1)

which is the forward-Euler discretisation of the same continuous system. It is a
first-order approximation of exp(-dt*A) and is stable exactly when dt*A < 1, which
the clamp enforces. In fixed point it is one multiply and one subtract. The cost
is accuracy at large dt; the training script constrains dt small enough that this
stays in the regime where the approximation is good, and `quantize.py` reports
float-vs-fixed agreement so the cost is measured rather than assumed.

This substitution is a claim in the writeup, not an implementation detail --
it changes the model, so the accuracy comparison must be against *this* model
trained this way, never against a Mamba baseline that used exp().
"""

import torch
import torch.nn as nn
import torch.nn.functional as F


class SelectiveSSM(nn.Module):
    """One selective state-space block: D channels, N states per channel."""

    def __init__(self, d_model, d_state):
        super().__init__()
        self.d_model = d_model
        self.d_state = d_state

        # A is per (channel, state) and time-invariant; selectivity enters
        # through dt, B and C. Stored as a raw parameter passed through softplus
        # so A stays strictly positive -- a negative A makes the recurrence
        # diverge, and nothing downstream would catch it.
        self.A_raw = nn.Parameter(torch.linspace(-2.0, 1.0, d_state)
                                  .repeat(d_model, 1).clone())
        self.D_skip = nn.Parameter(torch.ones(d_model))

        self.dt_proj = nn.Linear(d_model, d_model)
        self.B_proj = nn.Linear(d_model, d_state)
        self.C_proj = nn.Linear(d_model, d_state)
        self.out_proj = nn.Linear(d_model, d_model)

        # Start dt small: the Euler step is only a good approximation of the
        # exponential while dt*A << 1, and a large init walks straight into the
        # clamp where the gradient is zero and the block cannot learn.
        nn.init.constant_(self.dt_proj.bias, -4.0)

    def forward(self, x):
        """x: [B, T, D] -> [B, T, D]"""
        Bsz, T, D = x.shape
        N = self.d_state

        A = F.softplus(self.A_raw)                     # [D, N], > 0

        # unbind() once, rather than indexing [:, t] inside the loop. These are
        # equivalent forward, but indexing a [B,T,D,N] tensor T times makes
        # backward allocate and zero a full-size gradient buffer per step -- at
        # batch 256 that is 67 MB x 128 steps, and it made backward 14x the cost
        # of forward. Unbind lowers to a single stack in the backward pass.
        dts = F.softplus(self.dt_proj(x)).unbind(1)    # T x [B, D], > 0
        Bts = self.B_proj(x).unbind(1)                 # T x [B, N]
        Cts = self.C_proj(x).unbind(1)                 # T x [B, N]
        xs = x.unbind(1)                               # T x [B, D]

        # Sequential scan. Deliberately not parallelised into an associative
        # scan -- this loop is the exact computation the accelerator replaces,
        # so the reference should read the way the hardware works.
        h = x.new_zeros(Bsz, D, N)
        ys = []
        for t in range(T):
            dt_t, xt = dts[t], xs[t]                   # [B, D] each

            # a = 1 - dt*A, clamped into [0,1) for stability. The clamp is part
            # of the model, not a guard: the fixed-point port must clamp
            # identically or it will diverge on the same inputs.
            a = (1.0 - dt_t.unsqueeze(-1) * A).clamp(0.0, 0.999)   # [B, D, N]
            b = (dt_t * xt).unsqueeze(-1) * Bts[t].unsqueeze(1)    # [B, D, N]

            h = a * h + b
            y = (h * Cts[t].unsqueeze(1)).sum(-1)      # [B, D]
            ys.append(y + self.D_skip * xt)

        return self.out_proj(torch.stack(ys, dim=1))


class ECGNet(nn.Module):
    """input projection -> n_layers selective SSM -> mean pool -> linear head."""

    def __init__(self, d_model=32, d_state=16, n_layers=2, n_classes=5):
        super().__init__()
        self.in_proj = nn.Linear(1, d_model)
        self.blocks = nn.ModuleList(
            [SelectiveSSM(d_model, d_state) for _ in range(n_layers)])
        self.norms = nn.ModuleList(
            [nn.LayerNorm(d_model) for _ in range(n_layers)])
        # NO final LayerNorm before the head, deliberately. One was tried on
        # 2026-08-09 and reverted, because it is not the free win it looks like:
        #
        # The head reading the pooled vector *directly* is the only thing
        # penalising an unbounded residual stream -- weight decay on the head
        # has to pay for a large pooled scale. Normalising that scale away
        # removes the pressure entirely, and the stream promptly grew 4.4x
        # (residual peak 50.7 -> 221.4, y 106.6 -> 445.0, pooled 18.7 -> 77.5,
        # while the state h barely moved at 1.1x -- so it is the residual path,
        # not the scan).
        #
        # That is a *dynamic range* cost, not an op cost: the extra LayerNorm is
        # ~0.01% of the op budget and never touches the scan. It still dropped
        # float-vs-int agreement 97.3% -> 96.4% and pushed the activation scales
        # so coarse (res Q8.7, y Q9.6) that per-channel weight scales stopped
        # helping at all. Cheap in operations is not the same as cheap in bits.
        self.head = nn.Linear(d_model, n_classes)

    def forward(self, x):
        """x: [B, T] float in roughly [-1, 1] -> logits [B, n_classes]"""
        h = self.in_proj(x.unsqueeze(-1))
        for blk, norm in zip(self.blocks, self.norms):
            h = h + blk(norm(h))                       # pre-norm residual
        return self.head(h.mean(dim=1))


def param_count(model):
    return sum(p.numel() for p in model.parameters())


if __name__ == "__main__":
    for d_model in (16, 24, 32, 48):
        m = ECGNet(d_model=d_model)
        n = param_count(m)
        print("d_model=%-3d params=%5d  INT8=%6.1f KB  %s"
              % (d_model, n, n / 1024.0,
                 "OK" if n <= 10000 else "OVER 10k budget"))
