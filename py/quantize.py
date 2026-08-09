#!/usr/bin/env python3
"""
quantize.py -- INT8-weight / INT16-activation integer reference for ECGNet.

    py/venv/bin/python py/quantize.py --ckpt py/data/model_v2.pt --n 500

This is the artefact the C port must match **bit for bit** (STATUS.md §8 item 7).
It is not "the float model with rounding" -- it contains no float arithmetic in
the inference path at all. Float appears only when *choosing* the quantization
scales, which happens once, offline, in `calibrate()`.

Ordering matters and is easy to get backwards: the C port is validated against
*this*, and quantized-vs-float accuracy is reported **separately**. If the C port
were instead compared to the float model, a "bit-exact" claim would be vacuous,
because there is no bit-exact relationship between integer and float inference.

Scheme
------
* weights      int8, **per-output-channel** power-of-two scale
* activations  int16, **per-tensor** power-of-two scale (a separate number of
               fractional bits at each named site below)
* accumulators int32, overflow raises rather than wrapping
* state h      int16

Why both kinds of scale
-----------------------
Agreement with the float model, measured through *this* integer path (the
`--per-tensor-weights` and `--global-fa` flags reproduce each row):

    per-channel weights + per-tensor activation scales        97.3%
    per-TENSOR  weights + per-tensor activation scales        93.6%
    per-channel weights + one global scale, fa=12             62.4%
    per-channel weights + one global scale, fa=9              91.2%

Two independent problems, and fixing either alone is not enough:

1. **Activations.** A single scale cannot serve a +-1 input and a +-49 state in
   16 bits. This is not a tuning failure -- more range genuinely cuts saturation
   but costs more precision than it recovers. Hence one scale per site.
2. **Weights.** Per-tensor int8 costs ~4 points on its own, and no amount of
   activation tuning recovers it. A per-output-channel shift costs one extra
   indexed shift in the C port and buys those points back.

Note the large activations are **structural, not a training pathology**: the SSM
state integrates over ~100-130 timesteps, so it is legitimately ~40x the input.
Lowering the `a` clamp to shrink it was measured and rejected -- it leaves the
widest tensor untouched and collapses val macro-F1 from 0.564 to 0.165.

Three hardcoded shift constants mattered more than every scale
--------------------------------------------------------------
Getting the scales right took the agreement from 62% to only ~53%, because three
constants that were safe under a single global fa became destructive once fa
varied per tensor. Each was sized for the worst case of a quantity that is
nowhere near its worst case in practice:

* `SV` in LayerNorm, hardcoded 5, should be 3 for D=32 (derived from the int32
  bound). The residual stream sits at just 9 fractional bits, so `cen >> 5` left
  the variance 4 fractional bits and injected ~0.23 of error into a +-2 tensor.
* `isqrt(var)` with var ~2**13 in an int32 register returns ~96 and carries ~1%
  resolution. Normalising var to the top of int32 first recovers ~8 bits.
* `SP` in the scan, hardcoded 6, needs only `log2(N)-1` = 3 at N=16 -- three bits
  thrown away on every product in the innermost loop of the model.

Together these were worth 53% -> 97%, far more than any activation scale. The
lesson generalises: a shift constant chosen for a worst case that never occurs
is invisible in a working global-scale build and becomes the dominant error
source the moment the surrounding scales get tighter.

`softplus(A_raw)` is folded offline -- A is a parameter, not input-dependent, so
the positive A is exported directly and costs nothing at runtime. Only
`softplus(dt)` needs the table.
"""

import argparse
import os
import sys

import numpy as np
import torch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model import ECGNet                                   # noqa: E402
from fixedpoint import (INT16_MAX, INT16_MIN, check_int32,  # noqa: E402
                        linear_int, pow2_shift, quantize, rshift_round, sat,
                        softplus_int, softplus_lut)

HERE = os.path.dirname(os.path.abspath(__file__))

# The DS1 records held out for model selection in train.py; reused here so the
# quantizer is never measured on data the model was fitted to.
VAL_RECORDS = [106, 119, 209, 223]

# `a = 1 - dt*A` is clamped to [0, 0.999) by construction, so its range is known
# a priori and needs no calibration. 15 fractional bits puts 0.999 at 32735,
# just inside int16, and gives the decay the most resolution available -- it is
# multiplied into the state every timestep, so error here compounds over T.
FA_A = 15

# Sites that get their own activation scale. Calibrated from data except `a`.
#   res   the residual stream (in_proj output, and after each block add)
#   norm  LayerNorm output feeding the projections
#   dtp   dt_proj output BEFORE softplus (swings to ~-12)
#   dt    dt after softplus (peaks near 1 -- a different range entirely)
#   dtx   the dt*x intermediate inside the scan
#   bc    B and C projections
#   h     the SSM state
#   y     scan output before out_proj
#   pool  mean-pooled vector
#   nout  final LayerNorm output feeding the head
#   logit the head output -- reaches ~+-19, far outside the +-4 of `nout`
SITES = ("res", "norm", "dtp", "dt", "dtx", "bc", "h", "y", "pool", "nout", "logit")


def isqrt_int(n):
    """floor(sqrt(n)) by bit-by-bit restoring square root -- no division, no
    float. Directly portable to C with 32-bit ops."""
    n = np.asarray(n, dtype=np.int64)
    start = np.where(n > 0,
                     (np.floor(np.log2(np.maximum(n, 1))) // 2 * 2).astype(np.int64),
                     0)
    bit = (np.int64(1) << start)
    x = np.zeros_like(n)
    rem = n.copy()
    while (bit > 0).any():
        t = x + bit
        ge = rem >= t
        rem = np.where(ge, rem - t, rem)
        x = np.where(ge, (x >> 1) + bit, x >> 1)
        bit = bit >> 2
    return x


class Stats:
    """Counts the things that silently produce wrong answers on the core.

    Broken down per site: a total alone cannot distinguish "a few weights
    clipped during export" from "the state saturates on every timestep", and
    those need completely different fixes.
    """

    def __init__(self):
        self.sat = 0
        self.rclip = 0          # LayerNorm gain hit its bound (norm is wrong there)
        self.by_site = {}
        self.seen = {}          # elements passed through each site

    def add(self, site, n, total=0):
        self.sat += n
        if n:
            self.by_site[site] = self.by_site.get(site, 0) + n
        self.seen[site] = self.seen.get(site, 0) + total
        return n

    def __str__(self):
        if not self.by_site:
            return "saturations 0, layernorm-gain clips %d" % self.rclip
        worst = sorted(self.by_site.items(), key=lambda kv: -kv[1])
        parts = ["%s=%d (%.2f%%)"
                 % (k, v, 100.0 * v / max(self.seen.get(k, 1), 1)) for k, v in worst]
        return ("saturations %d [%s], layernorm-gain clips %d"
                % (self.sat, "  ".join(parts), self.rclip))


def layernorm_int(x_q, g_q, b_q, g_shift, fa_in, fa_out, st=None):
    """Integer LayerNorm over the last axis, fa_in -> fa_out fractional bits.

    Only ~2.8% of the op budget (doc/SESSION-2026-08-09.md §8), so this is
    written for clarity and exactness rather than speed. One integer sqrt and
    one division per timestep; CV32E40X has M so DIV is available.
    """
    D = x_q.shape[-1]
    acc = np.asarray(x_q, dtype=np.int64)

    # Mean: plain integer division. The obvious `sum * 2**fa / D` overflows
    # int32 (3.9e10) even though the sum itself is only 9.4e6.
    mean = check_int32(acc.sum(-1, keepdims=True), "layernorm sum") // D
    cen = acc - mean

    # Variance: sum(cen^2) over D=32 reaches 3.4e10 and does NOT fit in int32,
    # so the centred values are shifted down before squaring. SV is derived from
    # the int32 bound rather than hardcoded, and that matters a lot here:
    #
    #   |cen| <= 2**16  =>  sum_D (cen>>SV)**2 <= 2**(32-2*SV) * D <= 2**31-1
    #                   =>  SV >= (1 + log2(D)) / 2
    #
    # which is 3 for D=32, not the 5 this used to hardcode. Two bits sounds
    # minor and is not: with per-tensor scales the residual stream sits at only
    # 9 fractional bits (it is a +-50 tensor), so `cen >> 5` left the variance
    # just 4 fractional bits. That alone injected ~0.23 of error into a +-2
    # LayerNorm output -- the single largest error source in the whole integer
    # path, ahead of every activation and weight scale -- and it compounded
    # through both blocks. The old value was safe when fa was a global ~12 and
    # silently destructive once fa became per-tensor.
    SV = int(np.ceil((1 + np.log2(D)) / 2.0))
    cs = cen >> SV
    var = check_int32((cs * cs).sum(-1, keepdims=True) // D, "layernorm var")
    var = np.maximum(var, 1)

    # rsqrt without a 64-bit intermediate: isqrt first, then one divide.
    #   sq = isqrt(var << 2E)                 frac (fa_in-SV+E), = std
    #   r  = 2**(2*fa_in-SV+E) / sq           frac fa_in,        = 1/std
    # The naive 2**(3*fa)/var form needs 2**36 and would force int64 on RV32.
    #
    # `var` is typically only ~2**13 while int32 holds 2**31, so a plain
    # isqrt(var) returns ~96 and carries barely 1% resolution -- which lands
    # directly on the normalised output. Scaling var up to the top of int32
    # first (by an even power, so it comes out of the sqrt as a whole number of
    # bits) recovers that precision for the cost of a shift. E is derived from
    # the value, so it stays deterministic and bit-reproducible in C:
    #     while (var < (1 << 28)) { var <<= 2; E++; }
    bl = np.where(var > 0,
                  np.floor(np.log2(np.maximum(var, 1))).astype(np.int64) + 1, 1)
    # Two ceilings: var<<2E must fit int32, and so must the 1<<P dividend below.
    E = np.minimum((30 - bl) // 2, 30 - (2 * fa_in - SV))
    E = np.maximum(E, 0)
    sq = np.maximum(isqrt_int(check_int32(var << (2 * E), "layernorm var<<2E")), 1)
    r = check_int32((np.int64(1) << (2 * fa_in - SV + E)) // sq, "layernorm rsqrt")

    # Bound the gain so cen*r cannot overflow int32. This binds only when the
    # input std is below 1/8, where the normalisation would be wrong -- so it is
    # counted rather than left silent.
    rmax = np.int64(1) << (fa_in + 3)
    if st is not None:
        st.rclip += int((r > rmax).sum())
    r = np.clip(r, 1, rmax)

    # cen*r carries 2*fa_in fractional bits; land on fa_out in one shift.
    y = rshift_round(check_int32(cen * r, "layernorm scale"), 2 * fa_in - fa_out)
    y = rshift_round(y * np.asarray(g_q, np.int64), g_shift) + np.asarray(b_q, np.int64)
    y, n = sat(y, INT16_MIN, INT16_MAX)
    if st is not None:
        st.add("layernorm", n, y.size)
    return y.astype(np.int64)


@torch.no_grad()
def calibrate(model, X_cal, headroom=0, verbose=True):
    """Measure the peak magnitude at each site and pick fractional bits.

    Run on **DS1** data. Choosing scales on DS2 would be selecting a model
    hyper-parameter on the test set -- the same mistake as picking an
    architecture on the validation split (STATUS.md §7.4), one level down.
    """
    import torch.nn.functional as F
    peak = {s: 0.0 for s in SITES}

    def hit(site, t):
        peak[site] = max(peak[site], float(t.detach().abs().max()))

    x = torch.from_numpy(X_cal.astype(np.float32) / 32768.0)
    h = model.in_proj(x.unsqueeze(-1))
    hit("res", h)
    for blk, norm in zip(model.blocks, model.norms):
        nh = norm(h)
        hit("norm", nh)
        A = F.softplus(blk.A_raw)
        dt_pre = blk.dt_proj(nh)
        dts = F.softplus(dt_pre)
        hit("dtp", dt_pre)
        hit("dt", dts)
        Bt, Ct = blk.B_proj(nh), blk.C_proj(nh)
        hit("bc", Bt)
        hit("bc", Ct)
        du, Bu, Cu, xs = dts.unbind(1), Bt.unbind(1), Ct.unbind(1), nh.unbind(1)
        state = nh.new_zeros(nh.shape[0], blk.d_model, blk.d_state)
        ys = []
        for t in range(nh.shape[1]):
            a = (1.0 - du[t].unsqueeze(-1) * A).clamp(0.0, 0.999)
            dtx = du[t] * xs[t]
            hit("dtx", dtx)
            b = dtx.unsqueeze(-1) * Bu[t].unsqueeze(1)
            state = a * state + b
            hit("h", state)
            ys.append((state * Cu[t].unsqueeze(1)).sum(-1) + blk.D_skip * xs[t])
        yst = torch.stack(ys, 1)
        hit("y", yst)
        h = h + blk.out_proj(yst)
        hit("res", h)                     # block output and stream share a scale
    pooled = h.mean(1)
    hit("pool", pooled)
    if hasattr(model, "norm_out"):
        pooled = model.norm_out(pooled)
        hit("nout", pooled)
    hit("logit", model.head(pooled))

    fa = {}
    for s in SITES:
        # int16 holds +-32767; leave `headroom` bits of margin for unseen data.
        fa[s] = int(np.floor(np.log2(32767.0 / max(peak[s], 1e-9)))) - headroom
        fa[s] = int(np.clip(fa[s], 0, 15))
    fa["a"] = FA_A

    if verbose:
        print("[calib] %d beats, headroom %d bit(s)" % (len(X_cal), headroom))
        for s in SITES:
            print("        %-5s peak %9.3f  -> Q%d.%d  (range +-%.1f)"
                  % (s, peak[s], 15 - fa[s], fa[s], 32767 / 2.0 ** fa[s]))
    return fa, peak


class QuantECGNet:
    """Integer-only forward pass mirroring ECGNet."""

    def __init__(self, model, fa, per_channel=True):
        self.fa = fa
        self.L = len(model.blocks)
        self.st = Stats()
        sd = {k: v.detach().numpy().astype(np.float64)
              for k, v in model.state_dict().items()}
        self.q = {}
        self.lut = softplus_lut(fa["dtp"])

        def put_w(name, w, bits=8, per_channel=per_channel):
            """Weights: per-output-channel shift (axis=1 of a [out, in] matrix)."""
            s = pow2_shift(w, bits, axis=1 if (per_channel and w.ndim == 2) else None)
            qv, n = quantize(w, s, bits)
            self.st.add("weights:" + name, n, qv.size)
            self.q[name] = (qv, s)

        def put_b(name, b, w_shift, fa_in):
            """Bias lives at the accumulator scale, (fa_in + w_shift)."""
            self.q[name] = (np.rint(b * 2.0 ** (fa_in + np.asarray(w_shift))
                                    ).astype(np.int64), None)

        put_w("in_proj.w", sd["in_proj.weight"])
        put_b("in_proj.b", sd["in_proj.bias"], self.q["in_proj.w"][1], 15)

        # Which activation scale feeds each projection, so the bias matches it.
        feed = {"dt_proj": "norm", "B_proj": "norm", "C_proj": "norm",
                "out_proj": "y"}
        for i in range(self.L):
            p = "blocks.%d." % i
            # A is a parameter: fold softplus offline, export positive A.
            put_w(p + "A", np.log1p(np.exp(sd[p + "A_raw"])), 16, per_channel=False)
            put_w(p + "D_skip", sd[p + "D_skip"], 8, per_channel=False)
            for lin in ("dt_proj", "B_proj", "C_proj", "out_proj"):
                put_w(p + lin + ".w", sd[p + lin + ".weight"])
                put_b(p + lin + ".b", sd[p + lin + ".bias"],
                      self.q[p + lin + ".w"][1], fa[feed[lin]])
            n = "norms.%d." % i
            put_w(n + "w", sd[n + "weight"], 8, per_channel=False)
            self.q[n + "b"] = (np.rint(sd[n + "bias"] * 2.0 ** fa["norm"]
                                       ).astype(np.int64), None)

        self.has_norm_out = "norm_out.weight" in sd
        if self.has_norm_out:
            put_w("norm_out.w", sd["norm_out.weight"], 8, per_channel=False)
            self.q["norm_out.b"] = (np.rint(sd["norm_out.bias"] * 2.0 ** fa["nout"]
                                            ).astype(np.int64), None)
        head_in = "nout" if self.has_norm_out else "pool"
        self.head_in = head_in
        put_w("head.w", sd["head.weight"])
        put_b("head.b", sd["head.bias"], self.q["head.w"][1], fa[head_in])

    def _lin(self, x, name, fa_in, fa_out):
        w, ws = self.q[name + ".w"]
        b, _ = self.q[name + ".b"]
        y, n = linear_int(x, w, b, ws, fa_in, fa_out, what=name)
        self.st.add("lin:" + name.split(".")[-2] if "." in name else "lin:" + name, n, y.size)
        return y

    def block(self, x_q, i):
        """x_q: [B,T,D] at fa['norm'] -> [B,T,D] at fa['res']."""
        p = "blocks.%d." % i
        fa = self.fa
        A_q, A_s = self.q[p + "A"]
        Bsz, T, D = x_q.shape
        N = A_q.shape[1]

        # Pre- and post-softplus get separate scales. They look like one tensor
        # but are not: the pre-activation swings to about -12 (which sets the
        # scale) while softplus squashes everything negative towards 0, so the
        # output peaks near 1. Sharing a scale spent ~3 bits representing a range
        # the output never reaches, on the value that multiplies into the state
        # every timestep.
        dt_pre = self._lin(x_q, p + "dt_proj", fa["norm"], fa["dtp"])
        dt = rshift_round(softplus_int(dt_pre, self.lut, fa["dtp"]),
                          fa["dtp"] - fa["dt"])
        Bt = self._lin(x_q, p + "B_proj", fa["norm"], fa["bc"])
        Ct = self._lin(x_q, p + "C_proj", fa["norm"], fa["bc"])
        Dsk, Ds = self.q[p + "D_skip"]

        one = np.int64(1) << fa["a"]
        a_max = np.int64(int(0.999 * (1 << fa["a"])))

        # b = dt * x * B must land at fa['h']. Done in two shifts rather than
        # one: dt, x and B are all int16, so a single product would reach 3.5e13
        # and overflow int32 long before the final shift could bring it back.
        # The intermediate is brought back to int16 at its own calibrated scale,
        # which is what keeps dtx*B inside int32 (32767*32767 = 1.07e9).
        s_dtx = fa["dt"] + fa["norm"] - fa["dtx"]
        s_b = fa["dtx"] + fa["bc"] - fa["h"]

        h = np.zeros((Bsz, D, A_q.shape[1]), dtype=np.int64)
        ys = []
        for t in range(T):
            dt_t = dt[:, t][:, :, None]                     # [B,D,1]
            x_t = x_q[:, t][:, :, None]                     # [B,D,1]

            # a = 1 - dt*A, clamped to [0, 0.999) exactly as the float model.
            dtA = rshift_round(check_int32(dt_t * A_q[None, :, :], "dt*A"),
                               fa["dt"] + A_s - fa["a"])
            a = np.clip(one - dtA, 0, a_max)

            dtx = rshift_round(check_int32(dt_t * x_t, "dt*x"), s_dtx)
            dtx, ns = sat(dtx, INT16_MIN, INT16_MAX)
            self.st.add("dtx", ns, dtx.size)
            b = rshift_round(check_int32(dtx * Bt[:, t][:, None, :], "dt*x*B"), s_b)

            h = rshift_round(a * h, fa["a"]) + b
            h, ns = sat(h, INT16_MIN, INT16_MAX)            # INT16 state
            self.st.add("state h", ns, h.size)
            h = h.astype(np.int64)

            # y = sum_n C[n]*h[n]. Both are int16, so each product reaches 1.07e9
            # and the sum over N overflows int32 (measured 6.0e9 at N=16, worse
            # at N=64). Pre-shift each product before accumulating so the partial
            # sum stays inside int32:
            #     N * (2**15 * 2**15 >> SP) <= 2**31   =>   SP >= log2(N) - 1
            # which is 3 at N=16 and 5 at N=64. This was hardcoded at 6, three
            # bits more than N=16 needs, thrown away on every product in the
            # innermost loop of the whole model.
            SP = max(0, int(np.ceil(np.log2(N))) - 1)
            prod = check_int32(h * Ct[:, t][:, None, :], "scan product")
            acc = check_int32(rshift_round(prod, SP).sum(-1), "scan output")
            y = rshift_round(acc, fa["h"] + fa["bc"] - fa["y"] - SP)
            y = y + rshift_round(
                check_int32(np.asarray(Dsk, np.int64)[None, :] * x_q[:, t], "D_skip"),
                fa["norm"] + Ds - fa["y"])
            ys.append(y)

        y = np.stack(ys, 1)
        y, ns = sat(y, INT16_MIN, INT16_MAX)
        self.st.add("scan y", ns, y.size)
        return self._lin(y.astype(np.int64), p + "out_proj", fa["y"], fa["res"])

    def forward(self, x_int16):
        """x_int16: [B, T] raw int16 samples from beats.npz -> logits [B, 5]."""
        fa = self.fa
        # beats.npz is Q15 relative to full scale; in_proj takes it there.
        x = np.asarray(x_int16, np.int64)[:, :, None]
        h = self._lin(x, "in_proj", 15, fa["res"])

        for i in range(self.L):
            nm = "norms.%d." % i
            g, gs = self.q[nm + "w"]
            bb, _ = self.q[nm + "b"]
            hn = layernorm_int(h, g, bb, gs, fa["res"], fa["norm"], self.st)
            h = h + self.block(hn, i)
            h, ns = sat(h, INT16_MIN, INT16_MAX)
            self.st.add("residual", ns, h.size)
            h = h.astype(np.int64)

        # Divide before scaling: `h.sum(1) << fa` is 3.9e10 and overflows int32,
        # while the sum alone is only 9.4e6.
        pooled = check_int32(h.sum(1), "mean-pool accumulator") // h.shape[1]
        pooled = rshift_round(pooled, fa["res"] - fa["pool"])
        if self.has_norm_out:
            g, gs = self.q["norm_out.w"]
            bb, _ = self.q["norm_out.b"]
            pooled = layernorm_int(pooled, g, bb, gs, fa["pool"], fa["nout"], self.st)
        return self._lin(pooled, "head", fa[self.head_in], fa["logit"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", default=os.path.join(HERE, "data", "model_v2.pt"))
    ap.add_argument("--n", type=int, default=500, help="beats to compare")
    ap.add_argument("--test", action="store_true",
                    help="measure on DS2 instead of the DS1 holdout. Float-vs-int "
                         "agreement is a property of the quantizer, not of "
                         "generalisation, so DS1 answers it just as well and "
                         "keeps the test set unread (same reasoning as train.py)")
    ap.add_argument("--n-cal", type=int, default=256, help="DS1 beats to calibrate")
    ap.add_argument("--headroom", type=int, default=0,
                    help="extra bits of margin on every activation scale")
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--global-fa", type=int, default=None,
                    help="override: one scale everywhere (reproduces the old blocker)")
    ap.add_argument("--per-tensor-weights", action="store_true",
                    help="override: one weight scale per tensor instead of per channel")
    args = ap.parse_args()

    torch.set_num_threads(args.threads)
    ck = torch.load(args.ckpt, map_location="cpu", weights_only=False)
    cfg = ck["config"]
    model = ECGNet(cfg["d_model"], cfg["d_state"], cfg["n_layers"], cfg["n_classes"])
    # Checkpoints exist both with and without the final LayerNorm (it was added
    # and reverted on 2026-08-09 -- see model.py). Rebuild whichever this is,
    # rather than loading non-strictly and silently scoring a different model.
    if "norm_out.weight" in ck["state_dict"] and not hasattr(model, "norm_out"):
        import torch.nn as _nn
        model.norm_out = _nn.LayerNorm(cfg["d_model"])
        print("[quant] checkpoint carries the reverted final LayerNorm; "
              "reconstructing it to match")
    model.load_state_dict(ck["state_dict"])
    model.eval()
    print("[quant] %s  d_model=%d d_state=%d layers=%d  (val macro-F1 %.4f)"
          % (os.path.basename(args.ckpt), cfg["d_model"], cfg["d_state"],
             cfg["n_layers"], ck.get("val_macro_f1", float("nan"))))

    d = np.load(os.path.join(HERE, "data", "beats.npz"))

    # Calibrate on DS1 (training records), never on DS2.
    rng = np.random.RandomState(0)
    Xtr, ytr = d["X_train"], d["y_train"]
    cal_idx = np.concatenate([
        rng.choice(np.flatnonzero(ytr == c),
                   min(max(1, args.n_cal // 4), int((ytr == c).sum())), replace=False)
        for c in np.unique(ytr) if (ytr == c).sum() > 0])
    fa, _ = calibrate(model, Xtr[cal_idx], headroom=args.headroom)
    if args.global_fa is not None:
        fa = {k: args.global_fa for k in fa}
        print("[calib] OVERRIDDEN to a single global scale fa=%d" % args.global_fa)

    # Stratified, NOT the first n: both splits are ordered by record and record
    # 100 is nearly pure N, so a head slice compares the two models on a single
    # trivial class and reports ~100% regardless of correctness.
    #
    # Default to the DS1 holdout records. Nothing here needs DS2: agreement
    # compares two *implementations of the same model*, so the test set adds no
    # information and reading it only creates opportunities to select on it.
    if args.test:
        Xa, ya = d["X_test"], d["y_test"]
        split = "DS2"
    else:
        vm = np.isin(d["rec_train"], VAL_RECORDS)
        Xa, ya = d["X_train"][vm], d["y_train"][vm]
        split = "DS1 holdout"
    rng = np.random.RandomState(0)
    per = max(1, args.n // len(np.unique(ya)))
    idx = np.concatenate([rng.choice(np.flatnonzero(ya == c),
                                     min(per, int((ya == c).sum())), replace=False)
                          for c in np.unique(ya)])
    X, y = Xa[idx], ya[idx]
    print("[quant] %s sample: %s" % (split,
             " ".join("%s=%d" % (c, n) for c, n in zip(*np.unique(y, return_counts=True)))))

    with torch.no_grad():
        lf = model(torch.from_numpy(X.astype(np.float32) / 32768.0)).numpy()

    q = QuantECGNet(model, fa, per_channel=not args.per_tensor_weights)
    if args.per_tensor_weights:
        print("[quant] OVERRIDDEN to per-tensor weight scales")
    lq = np.asarray(q.forward(X), dtype=np.float64) / (2.0 ** fa["logit"])

    pf, pq = lf.argmax(1), lq.argmax(1)
    agree = (pf == pq).mean()
    print("[quant] logit max abs err %.4f, mean %.4f"
          % (np.abs(lf - lq).max(), np.abs(lf - lq).mean()))
    print("[quant] prediction agreement float vs int: %.2f%% (%d/%d)"
          % (100 * agree, (pf == pq).sum(), len(pf)))
    print("[quant] accuracy  float %.2f%%   int %.2f%%"
          % (100 * (pf == y).mean(), 100 * (pq == y).mean()))
    print("[quant] %s" % q.st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
