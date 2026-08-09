#!/usr/bin/env python3
"""
test_model.py -- guard the SSM scan against a naive reference implementation.

    py/venv/bin/python py/test_model.py

`SelectiveSSM.forward` is written for autograd efficiency (see the `unbind`
comment there -- the obvious version made backward 14x forward). Optimised code
that silently computes something slightly different is exactly the failure this
project cannot afford: it would be quantized, ported to C, and matched
bit-for-bit against the *wrong* model, with every check downstream passing.

So the fast path is checked against a deliberately stupid one written straight
from the recurrence:

    a_t = clamp(1 - dt_t * A, 0, 0.999)
    h_t = a_t * h_{t-1} + dt_t * B_t * x_t
    y_t = sum_n C_t[n] * h_t[n] + D * x_t
"""

import os
import sys

import torch
import torch.nn.functional as F

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model import SelectiveSSM, ECGNet, param_count      # noqa: E402


def naive_scan(blk, x):
    """The recurrence, one scalar element at a time. Slow and obvious."""
    Bsz, T, D = x.shape
    N = blk.d_state
    A = F.softplus(blk.A_raw)
    dt = F.softplus(blk.dt_proj(x))
    Bt = blk.B_proj(x)
    Ct = blk.C_proj(x)

    y = torch.zeros(Bsz, T, D)
    for b in range(Bsz):
        h = torch.zeros(D, N)
        for t in range(T):
            for d in range(D):
                acc = 0.0
                for n in range(N):
                    a = min(max(1.0 - dt[b, t, d].item() * A[d, n].item(), 0.0), 0.999)
                    h[d, n] = a * h[d, n] + dt[b, t, d].item() * Bt[b, t, n].item() * x[b, t, d].item()
                    acc += Ct[b, t, n].item() * h[d, n].item()
                y[b, t, d] = acc + blk.D_skip[d].item() * x[b, t, d].item()
    return blk.out_proj(y)


def main():
    torch.manual_seed(0)
    ok = True

    # Small enough that the triple-nested Python loop finishes.
    blk = SelectiveSSM(d_model=4, d_state=3)
    x = torch.randn(2, 6, 4)

    with torch.no_grad():
        fast = blk(x)
        slow = naive_scan(blk, x)
    err = (fast - slow).abs().max().item()
    print("scan vs naive reference: max abs err = %.3e" % err)
    if err > 1e-5:
        print("FAIL: fast scan does not match the recurrence")
        ok = False

    # The clamp must actually engage somewhere, or the test above proves nothing
    # about it. Force large dt and check the state stays bounded.
    with torch.no_grad():
        blk.dt_proj.bias.fill_(10.0)
        y = blk(torch.randn(2, 32, 4))
    if not torch.isfinite(y).all():
        print("FAIL: non-finite output with large dt -- clamp not holding")
        ok = False
    else:
        print("large-dt stability: finite, max |y| = %.3f" % y.abs().max().item())

    # Zero input must give exactly zero state, so any output is the bias path
    # only. Catches an accidental constant leaking into the scan.
    blk2 = SelectiveSSM(4, 3)
    with torch.no_grad():
        y0 = blk2(torch.zeros(1, 8, 4))
        expected = blk2.out_proj(torch.zeros(1, 8, 4))
    err0 = (y0 - expected).abs().max().item()
    print("zero input -> bias path only: max abs err = %.3e" % err0)
    if err0 > 1e-6:
        print("FAIL: zero input does not produce the zero-state output")
        ok = False

    n = param_count(ECGNet(32, 16, 2, 5))
    print("ECGNet(d_model=32) parameters = %d (%s 10k budget)"
          % (n, "within" if n <= 10000 else "OVER"))
    if n > 10000:
        ok = False

    print("\n%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
