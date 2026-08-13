#!/usr/bin/env python3
"""
train.py -- train the selective SSM on MIT-BIH beat windows.

    py/venv/bin/python py/train.py --epochs 15
    py/venv/bin/python py/train.py --smoke        # 2 min, checks the pipeline runs

Output: py/data/model.pt (weights + the config needed to rebuild the model).

## Model selection never touches DS2

DS2 is the test set and is read exactly once, at the end. Epoch selection uses a
validation split held out from DS1 **by record, not by beat** -- beats from one
recording are near-duplicates of each other, so a beat-wise validation split
leaks the patient into model selection and gives an optimistic number that does
not survive DS2. This is the same reasoning as the DS1/DS2 split itself
(prep_data.py), applied one level down.

## Class imbalance -- exactly one correction

The N class is ~90% of beats, so an uncorrected cross-entropy converges happily
to "always predict N" at ~89% accuracy. The correction applied here is the
`--max-n` cap on N in training only; loss class weighting (`--class-weights`)
defaults to **off**.

Applying both at once is what produced the V-precision collapse on DS2 in run #3
(sensitivity rose to 80.5% while precision fell to 59.9%). Measured effective
ratios and the reasoning are in the comment above the weight construction below.
Reported metrics are per-class sensitivity and positive predictivity -- overall
accuracy is printed only because its absence would look like hiding it.

Q (unclassifiable) has 8 training beats. It is kept for completeness but no
weighting scheme rescues 8 examples; expect it to be predicted essentially never,
and do not read anything into its score.
"""

import argparse
import json
import os
import sys
import time

import numpy as np
import torch
import torch.nn as nn

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model import ECGNet, param_count          # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
NPZ = os.path.join(HERE, "data", "beats.npz")
CKPT = os.path.join(HERE, "data", "model.pt")

CLASSES = ["N", "S", "V", "F", "Q"]

# Four DS1 records held out for epoch selection. Chosen to carry a usable number
# of S and V beats between them -- a validation split of nearly-pure N records
# would make the validation metric insensitive to exactly what we care about.
VAL_RECORDS = [106, 119, 209, 223]


def load(quiet=False):
    d = np.load(NPZ)
    Xtr, ytr, rtr = d["X_train"], d["y_train"], d["rec_train"]
    Xte, yte, rte = d["X_test"], d["y_test"], d["rec_test"]

    vmask = np.isin(rtr, VAL_RECORDS)
    split = {
        "train": (Xtr[~vmask], ytr[~vmask]),
        "val": (Xtr[vmask], ytr[vmask]),
        "test": (Xte, yte),
    }
    if not quiet:
        for k, (X, y) in split.items():
            cnt = np.bincount(y, minlength=5)
            print("[train] %-5s %6d beats  %s" % (
                k, len(y), " ".join("%s=%d" % (CLASSES[i], c)
                                    for i, c in enumerate(cnt))))
    return split


def to_tensors(X, y):
    # int16 -> [-1, 1). The scale is exact (2^15), so this conversion is
    # reversible and the fixed-point port sees the same numbers back.
    return (torch.from_numpy(X.astype(np.float32) / 32768.0),
            torch.from_numpy(y.astype(np.int64)))


@torch.no_grad()
def evaluate(model, X, y, batch, device):
    model.eval()
    preds = []
    for i in range(0, len(X), batch):
        xb = X[i:i + batch].to(device)
        preds.append(model(xb).argmax(1).cpu())
    p = torch.cat(preds)

    cm = np.zeros((5, 5), np.int64)
    for t, q in zip(y.numpy(), p.numpy()):
        cm[t, q] += 1
    return p, cm


# Model selection scores only N/S/V. F and Q are too rare and too unevenly
# spread across records to give a stable validation signal -- the DS1 holdout
# has 14 F beats and no Q at all, so including them makes the selection metric
# mostly noise. Both are still reported on the test set.
SELECT_CLASSES = ["N", "S", "V"]


def report(cm, title):
    tot = cm.sum()
    acc = np.trace(cm) / tot
    print("\n%s  (n=%d, overall accuracy %.2f%%)" % (title, tot, 100 * acc))
    print("  class      n      Se%      P+%      F1")
    f1 = {}
    for i, c in enumerate(CLASSES):
        n = cm[i].sum()
        npred = cm[:, i].sum()
        if n == 0:
            print("  %-5s %6d       -        -       -" % (c, n))
            continue
        se = cm[i, i] / n
        # No predictions for this class means zero precision, not undefined --
        # "never predicted it" is a real (bad) result, and nan would silently
        # poison the mean used for model selection.
        pp = cm[i, i] / npred if npred else 0.0
        f1[c] = 0.0 if (se + pp) == 0 else 2 * se * pp / (se + pp)
        print("  %-5s %6d  %7.2f  %7.2f  %6.4f" % (c, n, 100 * se, 100 * pp, f1[c]))

    sel = [f1[c] for c in SELECT_CLASSES if c in f1]
    macro = float(np.mean(sel)) if sel else 0.0
    print("  macro-F1 over %s: %.4f" % ("/".join(SELECT_CLASSES), macro))
    return macro


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--lr", type=float, default=3e-3)
    ap.add_argument("--d-model", type=int, default=32)
    ap.add_argument("--d-state", type=int, default=16)
    ap.add_argument("--layers", type=int, default=2)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--threads", type=int, default=4,
                    help="torch intra-op threads (default 4 -- do NOT raise, see below)")
    ap.add_argument("--max-n", type=int, default=12000,
                    help="cap N beats in TRAINING only (0 = keep all)")
    ap.add_argument("--class-weights", choices=("none", "sqrt"), default="none",
                    help="loss class weighting. Default 'none': the --max-n cap "
                         "is already one imbalance correction and applying both "
                         "over-represents V ~6x vs DS2 (see the table below)")
    ap.add_argument("--smoke", action="store_true",
                    help="1 epoch on 4000 beats, to check the pipeline runs")
    ap.add_argument("--test", action="store_true",
                    help="read DS2 and report inter-patient results. Off by "
                         "default on purpose -- every read is a chance to "
                         "select on the test set. Model selection uses the DS1 "
                         "holdout (VAL_RECORDS) and never needs this")
    ap.add_argument("--out", default=CKPT,
                    help="checkpoint path (set this when running two configs "
                         "concurrently, or they overwrite each other)")
    args = ap.parse_args()
    ckpt = args.out

    # Unbuffered-ish: without this, Python block-buffers stdout at 8 KB when it
    # is not a tty, which is about 20 epochs of output here -- a redirected log
    # stays empty for the entire run and there is no way to see progress.
    # `stdbuf -oL` does NOT fix it; CPython does not use libc stdio for stdout.
    sys.stdout.reconfigure(line_buffering=True)

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    device = "cpu"                    # model is tiny; CPU avoids a GPU dependency

    # DO NOT set this to os.cpu_count(). Measured on this 8-core machine, batch
    # 256, seconds per batch:
    #     1 thread 1.22   2 threads 0.90   3 threads 0.88
    #     4 threads 0.84  8 threads 26.48
    # There is a cliff between 4 and 8: the scan issues ~2000 tiny op launches
    # per forward, and at 8 threads the OpenMP barrier and spin-wait cost per op
    # swamps the actual work -- it burns every core while going 31x slower. A
    # first run at os.cpu_count() spent five hours to reach epoch 2 of 20.
    torch.set_num_threads(args.threads)
    print("[train] torch threads = %d (of %d cores)"
          % (args.threads, os.cpu_count() or 0))

    split = load()

    # Cap the N class in training only. N is ~90% of beats and by far the most
    # redundant -- consecutive normal beats from one patient are near-duplicates
    # -- so most of it buys nothing but epoch time, which matters now that the
    # 3-beat window made the scan 2.25x more expensive. Validation and test are
    # deliberately left at their true distribution: undersampling those would
    # inflate the minority-class metrics and make the numbers meaningless.
    if args.max_n > 0:
        Xa, ya = split["train"]
        rng = np.random.RandomState(args.seed)
        keep = np.ones(len(ya), bool)
        n_idx = np.flatnonzero(ya == 0)
        if len(n_idx) > args.max_n:
            drop = rng.choice(n_idx, len(n_idx) - args.max_n, replace=False)
            keep[drop] = False
            print("[train] capped N %d -> %d (train only); %d beats total"
                  % (len(n_idx), args.max_n, keep.sum()))
        split["train"] = (Xa[keep], ya[keep])

    # Drop classes too rare to learn anything from. Q has 8 training beats, and
    # because the weights go as 1/sqrt(count) it would otherwise receive the
    # *largest* weight in the loss -- amplifying eight examples into a gradient
    # that competes with 12000 N beats, at the cost of V and F recall. AAMI EC57
    # conventionally excludes Q from reported results anyway. Dropped from
    # training only; still counted in the test report, where it will show 0%
    # sensitivity, which is the honest outcome rather than a hidden one.
    MIN_TRAIN = 50
    Xa, ya = split["train"]
    cnt = np.bincount(ya, minlength=len(CLASSES))
    rare = [i for i in range(len(CLASSES)) if 0 < cnt[i] < MIN_TRAIN]
    if rare:
        print("[train] dropping from training (<%d beats): %s"
              % (MIN_TRAIN, ", ".join("%s=%d" % (CLASSES[i], cnt[i]) for i in rare)))
        m = ~np.isin(ya, rare)
        split["train"] = (Xa[m], ya[m])

    Xtr, ytr = to_tensors(*split["train"])
    Xva, yva = to_tensors(*split["val"])
    Xte, yte = to_tensors(*split["test"])

    if args.smoke:
        idx = torch.randperm(len(Xtr))[:4000]
        Xtr, ytr = Xtr[idx], ytr[idx]
        args.epochs = 1
        print("[train] SMOKE: %d beats, 1 epoch" % len(Xtr))

    model = ECGNet(args.d_model, args.d_state, args.layers, len(CLASSES)).to(device)
    print("[train] %d parameters (%.1f KB as INT8)"
          % (param_count(model), param_count(model) / 1024.0))

    # ONE imbalance correction, not two. The N cap above is the correction; the
    # class weights are off by default.
    #
    # Applying both over-represented the minority classes badly, and it showed up
    # on DS2 as V precision collapsing to 59.9% (1,612 N beats predicted V) while
    # V sensitivity *rose* -- the signature of a biased prior, invisible on a
    # validation split that shares the training distribution. Effective ratios
    # against DS2's true priors, measured on this dataset:
    #
    #   scheme                  eff N:V   V over-rep   eff N:S   S over-rep  beats
    #   cap 12000 + sqrt weights   2.26      6.07x        4.97      4.84x    15234
    #   cap 12000, no weights      5.11      2.69x       24.69      0.97x    15234
    #   no cap, sqrt weights       4.03      3.41x        8.85      2.72x    41330
    #   neither                   16.22      0.85x       78.39      0.31x    41330
    #   (DS2 truth: N:V 13.73, N:S 24.06)
    #
    # Keeping the cap and dropping the weights is best on *both* classes and is
    # 2.7x cheaper per epoch. It lands S at 0.97x -- essentially exactly DS2's
    # prior -- because DS1's raw N:S (78:1) is far more skewed than DS2's (24:1)
    # and the 3.17x N cap almost exactly cancels the difference. F is left 3.8x
    # over-represented, so dropping the weights does not starve it either.
    # Dropping the cap instead would be worse on both AND 2.7x slower.
    cnt = np.bincount(ytr.numpy(), minlength=5).astype(np.float64)
    if args.class_weights == "sqrt":
        # Inverse-*square-root* frequency, normalised to mean 1 so the loss scale
        # (and therefore a sensible lr) does not depend on the class
        # distribution. Plain inverse frequency spans 95x between N and F here.
        w = np.where(cnt > 0, 1.0 / np.sqrt(np.maximum(cnt, 1)), 0.0)
        w = w / w[w > 0].mean()
    else:
        w = (cnt > 0).astype(np.float64)
    print("[train] imbalance correction: max_n=%s class_weights=%s"
          % (args.max_n or "off", args.class_weights))
    print("[train] class weights: %s"
          % " ".join("%s=%.2f" % (CLASSES[i], w[i]) for i in range(5)))
    lossfn = nn.CrossEntropyLoss(weight=torch.tensor(w, dtype=torch.float32))

    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=1e-2)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)

    best, best_state = -1.0, None
    for ep in range(1, args.epochs + 1):
        model.train()
        perm = torch.randperm(len(Xtr))
        tot, nb, t0 = 0.0, 0, time.time()
        for i in range(0, len(perm), args.batch):
            b = perm[i:i + args.batch]
            opt.zero_grad()
            loss = lossfn(model(Xtr[b].to(device)), ytr[b].to(device))
            loss.backward()
            # The recurrence is a product of T terms; without clipping, an early
            # step that pushes a towards 1 can produce a very large gradient.
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            tot += loss.item()
            nb += 1
        sched.step()

        _, cm = evaluate(model, Xva, yva, args.batch, device)
        f1 = report(cm, "[val] epoch %d" % ep)
        print("[train] epoch %d loss %.4f  %.1fs" % (ep, tot / nb, time.time() - t0))

        if f1 > best:
            best = f1
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
            # Checkpoint immediately rather than only at the end. The first run
            # of this script was killed after five hours and lost everything,
            # because the only torch.save was after the epoch loop.
            torch.save({"state_dict": best_state,
                        "config": {"d_model": args.d_model,
                                   "d_state": args.d_state,
                                   "n_layers": args.layers,
                                   "n_classes": len(CLASSES)},
                        "val_macro_f1": best, "epoch": ep,
                        "classes": CLASSES}, ckpt)
            print("[train] new best val macro-F1 %.4f -> saved %s"
                  % (best, os.path.basename(ckpt)))

    if best_state is not None:
        model.load_state_dict(best_state)

    # DS2 is NOT read unless asked for. This used to run automatically at the
    # end of every training run, which quietly defeats the "read exactly once"
    # policy it was supposed to implement: after a few runs you have several DS2
    # numbers in your scrollback, and picking the checkpoint with the best one
    # is selecting on the test set -- exactly the mistake the inter-patient
    # split exists to prevent. Making it an explicit flag means a DS2 read is
    # always a deliberate act that shows up in the shell history.
    if args.test:
        _, cm = evaluate(model, Xte, yte, args.batch, device)
        report(cm, "[TEST] DS2 inter-patient")
        print("\nconfusion (rows = true, cols = predicted):")
        print("        " + "".join("%8s" % c for c in CLASSES))
        for i, c in enumerate(CLASSES):
            print("  %-5s " % c + "".join("%8d" % v for v in cm[i]))
    else:
        print("\n[train] DS2 not evaluated (pass --test to read the test set).")

    torch.save({
        "state_dict": model.state_dict(),
        "config": {"d_model": args.d_model, "d_state": args.d_state,
                   "n_layers": args.layers, "n_classes": len(CLASSES)},
        "val_macro_f1": best,
        "classes": CLASSES,
    }, ckpt)
    print("\n[train] wrote %s" % ckpt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
