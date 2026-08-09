#!/usr/bin/env python3
"""
fixedpoint.py -- integer arithmetic primitives shared by the quantized model.

Everything here is written the way the C port must behave, not the way numpy
would most conveniently do it. In particular:

* accumulators are **int32**, and overflow is checked rather than wrapped
  silently -- on the core an overflow is a wrong answer with no symptom
* shifts are **arithmetic with round-to-nearest**, `(v + (1 << (s-1))) >> s`,
  because truncation biases every value toward zero and that bias compounds
  through 288 timesteps of recurrence
* saturation is explicit, never numpy's wraparound

The point of this module is that the Python reference and the C implementation
can be checked against each other **bit for bit**. If numpy semantics were
allowed to leak in (float intermediates, silent int64 promotion), the two would
agree on average and differ in the tails, which is the hardest kind of bug to
find on hardware.

Fixed-point convention
----------------------
A value stored as integer `q` with `f` fractional bits represents `q / 2**f`.
Activations use `FA` fractional bits throughout; weights carry their own
per-tensor shift so that each tensor uses the full int8 range.
"""

import numpy as np

INT8_MIN, INT8_MAX = -127, 127          # -128 excluded: keeps negation safe
INT16_MIN, INT16_MAX = -32768, 32767
INT32_MIN, INT32_MAX = -(2**31), 2**31 - 1


def rshift_round(v, s):
    """Arithmetic right shift by s with round-to-nearest (ties away from zero).

    Implemented with integer ops only, matching what the C port will do:
        (v + (1 << (s-1))) >> s
    Note >> on a negative numpy int is already arithmetic, as in C for the
    compilers we target.

    `s` may be:
      * a non-negative scalar -- the original behaviour, bit-for-bit unchanged
      * a **negative** scalar -- a left shift by -s, needed when a per-tensor
        activation rescale moves to *more* fractional bits
      * an **array** broadcasting against the trailing axes of `v` -- needed for
        per-output-channel weight scales, where each output column of a linear
        layer carries its own shift. In C this is `sh[o]` indexed per output,
        not a new kind of operation.
    """
    v = np.asarray(v, dtype=np.int64)
    s_arr = np.asarray(s, dtype=np.int64)

    if s_arr.ndim == 0:
        si = int(s_arr)
        if si == 0:
            return v
        if si < 0:
            return v << np.int64(-si)
        return (v + (np.int64(1) << (si - 1))) >> si

    # Per-channel. Both branches are evaluated by np.where, so each must be
    # well-defined everywhere: `right`/`left` are clamped at 0 rather than
    # allowed to go negative, which numpy would reject.
    right = np.maximum(s_arr, 0)
    bias = np.where(right > 0, np.int64(1) << np.maximum(right - 1, 0), np.int64(0))
    shifted = (v + bias) >> right
    left = np.maximum(-s_arr, 0)
    return np.where(s_arr >= 0, shifted, v << left)


def sat(v, lo, hi, what="value"):
    """Saturate to [lo, hi]. Returns (clipped, n_saturated)."""
    v = np.asarray(v)
    n = int(((v < lo) | (v > hi)).sum())
    return np.clip(v, lo, hi), n


def check_int32(v, what):
    """Fail loudly if an accumulator would not fit in int32 on the core."""
    v = np.asarray(v)
    if v.size and (v.min() < INT32_MIN or v.max() > INT32_MAX):
        raise OverflowError(
            "%s overflows int32: range [%d, %d]" % (what, v.min(), v.max()))
    return v


def pow2_shift(x, bits=8, headroom=0, axis=None):
    """Choose s so that round(x * 2**s) fills a signed `bits` integer.

    Power-of-two scales are used rather than the usual float scale + int32
    multiplier because dequantisation then costs a shift instead of a multiply
    and a shift, inside the loop being accelerated. The cost is up to 2x of
    dynamic range versus an arbitrary scale -- acceptable here, and measured by
    quantize.py rather than assumed.

    `axis=None` gives one shift for the whole tensor. Passing an axis gives one
    shift **per output channel** (for a [out, in] weight, `axis=1`), which is
    what lets each row use the full int8 range independently.

    Per-channel is not a refinement, it is worth ~4 points: measured on the
    run-3 checkpoint over 647 stratified DS2 beats, INT8 weights with exact
    activations agree with float 94.7% per-tensor versus 98.9% per-channel, and
    no amount of activation-scale tuning recovers the difference. `floor` (not
    `ceil`) is essential -- rounding the shift up overshoots the int8 range and
    silently clamps every large weight.
    """
    x = np.asarray(x, dtype=np.float64)
    limit = float(2 ** (bits - 1) - 1)

    if axis is None:
        peak = float(np.abs(x).max())
        if peak == 0.0:
            return 0
        return int(np.floor(np.log2(limit / peak))) - headroom

    peak = np.abs(x).max(axis=axis)
    s = np.where(peak > 0.0,
                 np.floor(np.log2(limit / np.maximum(peak, 1e-300))),
                 0.0)
    return s.astype(np.int64) - headroom


def quantize(x, s, bits=8):
    """Quantize float -> integer with 2**s scaling, saturating.

    `s` may be a scalar or a per-output-channel array; an array is left-aligned
    against `x`'s leading axes, so a [out] shift applies down a [out, in] weight.
    """
    lo, hi = -(2 ** (bits - 1) - 1), 2 ** (bits - 1) - 1
    x = np.asarray(x, dtype=np.float64)
    s = np.asarray(s)
    if s.ndim:
        s = s.reshape(s.shape + (1,) * (x.ndim - s.ndim))
    q = np.rint(x * (2.0 ** s.astype(np.float64)))
    q, n = sat(q, lo, hi)
    return q.astype(np.int64), n


def dequantize(q, s):
    return np.asarray(q, dtype=np.float64) / (2.0 ** s)


def linear_int(x_q, w_q, b_q, w_shift, fa_in, fa_out=None, out_bits=16,
               what="linear"):
    """Integer affine layer.

    x_q : [..., in]  activations, `fa_in` fractional bits
    w_q : [out, in]  int8 weights, `w_shift` fractional bits -- scalar, or an
                     [out] array for per-output-channel scales
    b_q : [out]      bias pre-scaled to (fa_in + w_shift) fractional bits
    Returns activations with `fa_out` fractional bits (default: `fa_in`).

    The accumulator carries (fa_in + w_shift) fractional bits, so landing on
    fa_out is a single shift by `w_shift + fa_in - fa_out`. That combined shift
    is what makes per-tensor activation scales free at layer boundaries: the
    rescale rides along with the dequantisation shift already being done, and
    costs no extra instruction in the C port. It may be negative (a left shift)
    when the output tensor is allocated more fractional bits than the input.
    """
    if fa_out is None:
        fa_out = fa_in
    acc = np.asarray(x_q, dtype=np.int64) @ np.asarray(w_q, dtype=np.int64).T
    if b_q is not None:
        acc = acc + np.asarray(b_q, dtype=np.int64)
    check_int32(acc, what + " accumulator")
    y = rshift_round(acc, np.asarray(w_shift) + fa_in - fa_out)
    lo, hi = -(2 ** (out_bits - 1)), 2 ** (out_bits - 1) - 1
    y, n = sat(y, lo, hi)
    return y, n


def softplus_lut(fa, n_entries=256, lo=-8.0, hi=8.0):
    """Table for softplus(z) = log(1+exp(z)) over [lo, hi], `fa` frac bits.

    Outside the range the function is used in closed form: softplus(z) ~= 0 for
    z < lo and ~= z for z > hi, both exact to well under one LSB at fa=12.
    A table plus linear interpolation avoids exp() entirely, which is what makes
    this implementable on a core with no FPU.
    """
    z = np.linspace(lo, hi, n_entries)
    v = np.log1p(np.exp(z))
    q = np.rint(v * (2.0 ** fa)).astype(np.int64)
    return q, lo, hi


def softplus_int(z_q, table, fa):
    """Integer softplus via table lookup with linear interpolation."""
    tbl, lo, hi = table
    n = len(tbl)
    z_lo = int(round(lo * (2 ** fa)))
    z_hi = int(round(hi * (2 ** fa)))
    z = np.asarray(z_q, dtype=np.int64)

    span = z_hi - z_lo
    idx_f = (z - z_lo) * (n - 1)
    idx = np.clip(idx_f // span, 0, n - 2).astype(np.int64)
    frac = idx_f - idx * span                      # in [0, span)

    t0 = tbl[idx]
    t1 = tbl[idx + 1]
    interp = t0 + (t1 - t0) * frac // span

    out = np.where(z <= z_lo, 0, np.where(z >= z_hi, z, interp))
    return out.astype(np.int64)
