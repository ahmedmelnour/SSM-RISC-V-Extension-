#!/usr/bin/env python3
"""
test_fixedpoint.py -- guards the integer primitives.

    py/venv/bin/python py/test_fixedpoint.py

These primitives are the contract between `quantize.py` and the C port. The
whole "bit for bit" claim in STATUS.md §8 rests on them behaving exactly as C
would, so the properties that the C port relies on are asserted here rather than
assumed:

* a per-channel shift must equal applying the scalar shift channel by channel
  -- otherwise the vectorised Python reference and a scalar C loop diverge
* `pow2_shift` must never choose a scale that saturates the tensor it was
  derived from (the `floor` vs `ceil` trap -- `ceil` overshoots the int8 range
  and silently clamps every large weight)
* rounding must be symmetric about zero, because a truncation bias compounds
  through 288 timesteps of recurrence
"""

import sys
import numpy as np

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from fixedpoint import (INT8_MAX, INT32_MAX, check_int32,  # noqa: E402
                        linear_int, pow2_shift, quantize, rshift_round, sat)

rng = np.random.RandomState(0)
fails = []


def check(name, ok, detail=""):
    print("  %-58s %s%s" % (name, "PASS" if ok else "FAIL",
                            "" if ok else "  <- " + detail))
    if not ok:
        fails.append(name)


print("rshift_round")

# The exact formula the C port will use, including on negatives.
v = rng.randint(-2**30, 2**30, size=5000).astype(np.int64)
for s in (1, 3, 8, 15):
    ref = (v + (np.int64(1) << (s - 1))) >> s
    check("scalar shift %2d matches (v + (1<<(s-1))) >> s" % s,
          np.array_equal(rshift_round(v, s), ref))

check("shift 0 is identity", np.array_equal(rshift_round(v, 0), v))
check("negative shift is a left shift",
      np.array_equal(rshift_round(v, -4), v << 4))

# Rounding must not be biased toward zero: exactly-representable halves go away
# from zero on both sides, symmetrically.
half = np.array([-3, -1, 1, 3], dtype=np.int64)      # -1.5, -0.5, 0.5, 1.5 at s=1
check("rounding is symmetric about zero",
      np.array_equal(rshift_round(half, 1), np.array([-1, 0, 1, 2])),
      str(rshift_round(half, 1)))

# The property the C port depends on: a vector of shifts == scalar per element.
A = rng.randint(-2**28, 2**28, size=(64, 9)).astype(np.int64)
sh = rng.randint(-3, 12, size=9).astype(np.int64)
percol = np.stack([rshift_round(A[:, j], int(sh[j])) for j in range(9)], axis=1)
check("array shift == scalar shift applied per channel",
      np.array_equal(rshift_round(A, sh), percol))


print("\npow2_shift / quantize")

for bits in (8, 16):
    for trial in range(200):
        w = rng.randn(rng.randint(1, 12), rng.randint(1, 12)) * 10 ** rng.uniform(-3, 2)
        s = pow2_shift(w, bits)
        q, nsat = quantize(w, s, bits)
        if nsat != 0:
            check("per-tensor pow2_shift never saturates (bits=%d)" % bits, False,
                  "%d saturated" % nsat)
            break
    else:
        check("per-tensor pow2_shift never saturates (bits=%d)" % bits, True)

for bits in (8, 16):
    bad = 0
    for trial in range(200):
        w = rng.randn(rng.randint(2, 12), rng.randint(2, 12)) * 10 ** rng.uniform(-3, 2)
        s = pow2_shift(w, bits, axis=1)
        q, nsat = quantize(w, s, bits)
        bad += (nsat != 0)
    check("per-channel pow2_shift never saturates (bits=%d)" % bits, bad == 0,
          "%d/200 tensors saturated" % bad)

# Per-channel must be at least as tight as per-tensor: every row gets a shift
# no smaller than the global one, so no row loses range.
w = rng.randn(16, 7)
check("per-channel shift >= per-tensor shift, elementwise",
      bool((pow2_shift(w, 8, axis=1) >= pow2_shift(w, 8)).all()))

# ...and strictly better somewhere, which is the entire point.
check("per-channel is strictly tighter for a spread tensor",
      bool((pow2_shift(w, 8, axis=1) > pow2_shift(w, 8)).any()))

# quantize must broadcast a [out] shift down a [out, in] weight, not across it.
# Values chosen to stay inside int8 so this tests broadcasting alone; the
# saturation path is covered separately above.
w = np.array([[1.0, 2.0], [100.0, 120.0]])
q, nsat = quantize(w, np.array([5, 0]), 8)
check("per-channel quantize broadcasts along output rows",
      np.array_equal(q, np.array([[32, 64], [100, 120]])) and nsat == 0, str(q))

# A row whose shift is too large must saturate, and be reported.
q, nsat = quantize(np.array([[1.0], [200.0]]), np.array([0, 0]), 8)
check("per-channel quantize reports saturation per row",
      np.array_equal(q, np.array([[1], [127]])) and nsat == 1, str(q))


print("\nlinear_int")

x = rng.randint(-2000, 2000, size=(7, 6)).astype(np.int64)
wq = rng.randint(-INT8_MAX, INT8_MAX, size=(5, 6)).astype(np.int64)
bq = rng.randint(-5000, 5000, size=5).astype(np.int64)

# fa_in == fa_out reproduces the original single-shift behaviour exactly.
acc = x @ wq.T + bq
y, _ = linear_int(x, wq, bq, 7, 8)
check("fa_in == fa_out is the plain w_shift dequantisation",
      np.array_equal(y, rshift_round(acc, 7)))

# Changing fa rides along in the same shift. out_bits is widened here so this
# checks the shift arithmetic rather than the saturation that would otherwise
# mask it -- acc reaches ~1.5e6 and would clip against int16.
y2, _ = linear_int(x, wq, bq, 7, 8, fa_out=5, out_bits=32)
check("fa_out < fa_in shifts by w_shift + fa_in - fa_out",
      np.array_equal(y2, rshift_round(acc, 7 + 8 - 5)))
y3, _ = linear_int(x, wq, bq, 7, 8, fa_out=11, out_bits=32)
check("fa_out > fa_in reduces the shift",
      np.array_equal(y3, rshift_round(acc, 7 + 8 - 11)))

# When fa_out exceeds w_shift + fa_in the combined shift goes negative and must
# become a left shift, which is the case a right-shift-only implementation gets
# silently wrong.
y4, _ = linear_int(x, wq, bq, 7, 8, fa_out=20, out_bits=32)
check("negative combined shift becomes a left shift",
      np.array_equal(y4, acc << 5))

# Per-channel weight shift == running each output channel on its own.
shc = np.array([4, 6, 7, 5, 8], dtype=np.int64)
ypc, _ = linear_int(x, wq, bq, shc, 8)
manual = np.stack([rshift_round(acc[:, o], int(shc[o])) for o in range(5)], axis=1)
check("per-channel linear == per-channel scalar loop",
      np.array_equal(ypc, manual))

# Saturation is reported, not silently absorbed.
big = np.full((2, 6), 30000, dtype=np.int64)
_, nsat = linear_int(big, wq, None, 0, 8, out_bits=16)
check("out_bits saturation is counted", nsat > 0)


print("\ncheck_int32")
try:
    check_int32(np.array([INT32_MAX + 1], dtype=np.int64), "test")
    check("overflow raises instead of wrapping", False, "no exception")
except OverflowError:
    check("overflow raises instead of wrapping", True)
check("in-range passes through",
      np.array_equal(check_int32(np.array([5, -5]), "t"), np.array([5, -5])))

clipped, n = sat(np.array([-5, 0, 5]), -1, 1)
check("sat clips and counts", np.array_equal(clipped, [-1, 0, 1]) and n == 2)


print("\n%s" % ("PASS" if not fails else "FAIL: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
