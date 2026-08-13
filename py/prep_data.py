#!/usr/bin/env python3
"""
prep_data.py -- MIT-BIH recordings -> fixed-point beat windows for the SSM.

    py/venv/bin/python py/prep_data.py

Output: py/data/beats.npz (+ beats.json describing exactly how it was made).

This produces the *inputs* that every later stage shares -- the float model, the
quantized model, the fixed-point Python reference and the C port all consume the
same int16 windows. That is deliberate: the bit-exactness claim in the thesis is
"C matches the fixed-point reference", and it is only meaningful if neither side
gets to re-derive its own inputs.

Design decisions that are easy to get wrong, and why they are what they are:

* **Inter-patient split (DS1 train / DS2 test), not a random beat split.**
  This is the de Chazal 2004 division, and it is the single thing that most
  affects whether the accuracy number means anything. MIT-BIH beats are highly
  correlated within a recording, so a random split puts near-duplicate beats
  from the same patient on both sides and reports ~99% accuracy that collapses
  on a new patient. An examiner who knows the dataset will check for this first.

* **Paced records (102, 104, 107, 217) excluded.** Required by AAMI EC57.

* **Channel selected by name, not by index.** MLII is channel 0 in most records
  but channel 1 in record 114. Hardcoding channel 0 silently trains on a
  different lead for that record.

* **Annotated R-peaks are used for segmentation, not a detector.** Standard
  practice for this benchmark, but it means the reported cost excludes QRS
  detection -- see the note in STATUS.md about the frontend. State this in the
  writeup rather than letting it be discovered.

* **The window spans about three beats, not one.** This is the single most
  consequential parameter here. A one-beat window (128 samples at 180 Hz) trains
  a model that detects V beats well -- 68% sensitivity after one epoch -- and
  detects S beats *not at all*, 0.00% forever. That is not a training failure,
  it is the framing: supraventricular ectopic beats are largely normal in
  morphology, and what identifies them is that they arrive **early**. Centring a
  short window on the R-peak deletes exactly that information. de Chazal's
  original work feeds explicit RR-interval features for this reason.

  Rather than bolt hand-engineered RR features onto the input, the window is
  widened to ~2.4 s so the neighbouring beats are inside it and the sequence
  model can see the rhythm itself. That keeps the SSM as the thing doing the
  work, which is the point of the project -- and it makes T larger, so the scan
  the accelerator targets gets bigger rather than smaller.

  The cost is real: scan work is O(T), so T=288 is 2.25x the compute of T=128
  per inference. Decimation is raised from 2 to 3 (120 Hz) to hold that down
  while keeping Nyquist at 60 Hz, comfortably above the ~40 Hz where QRS energy
  lives.

* **Downsampling and baseline removal happen here, not on the core.** The
  measured kernel is the SSM scan; keeping the frontend in Python keeps the
  measurement about the thing being accelerated.
"""

import argparse
import json
import os
import sys
from collections import Counter

import numpy as np
import wfdb
from scipy import signal as sps

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data", "mitdb")
OUT_NPZ = os.path.join(HERE, "data", "beats.npz")
OUT_JSON = os.path.join(HERE, "data", "beats.json")

# de Chazal et al. 2004 inter-patient division. 22 records each, 44 total:
# the 48 recordings minus the four paced ones.
DS1 = ["101", "106", "108", "109", "112", "114", "115", "116", "118", "119",
       "122", "124", "201", "203", "205", "207", "208", "209", "215", "220",
       "223", "230"]
DS2 = ["100", "103", "105", "111", "113", "117", "121", "123", "200", "202",
       "210", "212", "213", "214", "219", "221", "222", "228", "231", "232",
       "233", "234"]

# AAMI EC57 grouping of the MIT-BIH beat annotation symbols.
AAMI = {
    "N": ["N", "L", "R", "e", "j"],   # normal + bundle branch blocks + atrial escape
    "S": ["A", "a", "J", "S"],        # supraventricular ectopic
    "V": ["V", "E"],                  # ventricular ectopic
    "F": ["F"],                       # fusion of ventricular and normal
    "Q": ["/", "f", "Q"],             # paced / fusion-with-paced / unclassifiable
}
CLASSES = ["N", "S", "V", "F", "Q"]
SYM2CLS = {s: i for i, c in enumerate(CLASSES) for s in AAMI[c]}

FS_NATIVE = 360        # MIT-BIH sampling rate, Hz

# 1 mV -> 8192 counts, i.e. millivolts in Q3.13. A power-of-two scale keeps the
# eventual fixed-point arithmetic to shifts, and +-4 mV covers MIT-BIH's range
# with headroom (a large QRS is ~1-2 mV).
MV_SCALE = 8192
MV_CLIP = 32767


def load_record(rec, win, decim):
    """Return (windows int16 [n, win], labels int8 [n]) for one recording."""
    path = os.path.join(DATA_DIR, rec)
    sig = wfdb.rdrecord(path)
    ann = wfdb.rdann(path, "atr")

    # Select MLII by name. Record 114 has it in channel 1.
    names = [n.strip() for n in sig.sig_name]
    ch = names.index("MLII") if "MLII" in names else 0

    x = sig.p_signal[:, ch].astype(np.float64)   # physical units, mV

    # Anti-aliased decimation. FIR (zero-phase, filtfilt) so the R-peak sample
    # indices stay aligned after downsampling -- an IIR phase shift here would
    # move the peak off-centre in every window by a few samples.
    if decim > 1:
        x = sps.decimate(x, decim, ftype="fir", zero_phase=True)

    peaks = ann.sample // decim
    syms = ann.symbol

    half = win // 2
    out, lab = [], []
    for p, s in zip(peaks, syms):
        cls = SYM2CLS.get(s)
        if cls is None:
            continue                       # non-beat annotation (rhythm, noise, ...)
        a, b = p - half, p + (win - half)
        if a < 0 or b > len(x):
            continue                       # window would run off the record
        w = x[a:b]
        w = w - np.median(w)               # per-window baseline removal
        q = np.clip(np.rint(w * MV_SCALE), -MV_CLIP, MV_CLIP)
        out.append(q.astype(np.int16))
        lab.append(cls)

    if not out:
        return np.zeros((0, win), np.int16), np.zeros((0,), np.int8)
    return np.stack(out), np.array(lab, np.int8)


def build(records, win, decim, tag):
    X, y, src = [], [], []
    for rec in records:
        if not os.path.exists(os.path.join(DATA_DIR, rec + ".dat")):
            print("[prep] %s: MISSING -- run py/fetch_data.py" % rec)
            continue
        xr, yr = load_record(rec, win, decim)
        X.append(xr)
        y.append(yr)
        src.append(np.full(len(yr), int(rec), np.int16))
        print("[prep] %s %-5s %5d beats  %s"
              % (tag, rec, len(yr),
                 " ".join("%s=%d" % (CLASSES[i], c)
                          for i, c in sorted(Counter(yr.tolist()).items()))))
    return (np.concatenate(X), np.concatenate(y), np.concatenate(src))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--win", type=int, default=288,
                    help="samples per window (default 288 = 2.4 s at 120 Hz, ~3 beats)")
    ap.add_argument("--decim", type=int, default=3,
                    help="decimation factor from 360 Hz (default 3 -> 120 Hz)")
    args = ap.parse_args()

    fs_out = FS_NATIVE // args.decim
    print("[prep] window %d samples @ %d Hz = %.3f s, int16 mV*%d"
          % (args.win, fs_out, args.win / fs_out, MV_SCALE))

    Xtr, ytr, str_ = build(DS1, args.win, args.decim, "DS1")
    Xte, yte, ste = build(DS2, args.win, args.decim, "DS2")

    np.savez_compressed(OUT_NPZ,
                        X_train=Xtr, y_train=ytr, rec_train=str_,
                        X_test=Xte, y_test=yte, rec_test=ste,
                        classes=np.array(CLASSES))

    meta = {
        "window_samples": args.win,
        "decimation": args.decim,
        "fs_hz": fs_out,
        "mv_scale": MV_SCALE,
        "dtype": "int16",
        "split": "de Chazal 2004 inter-patient (DS1 train / DS2 test)",
        "classes": CLASSES,
        "train_records": DS1,
        "test_records": DS2,
        "n_train": int(len(ytr)),
        "n_test": int(len(yte)),
        "train_class_counts": {CLASSES[i]: int(c) for i, c in
                               sorted(Counter(ytr.tolist()).items())},
        "test_class_counts": {CLASSES[i]: int(c) for i, c in
                              sorted(Counter(yte.tolist()).items())},
    }
    with open(OUT_JSON, "w") as f:
        json.dump(meta, f, indent=2)

    print("\n[prep] train %d beats, test %d beats" % (len(ytr), len(yte)))
    for name, yy in (("train", ytr), ("test", yte)):
        cnt = Counter(yy.tolist())
        tot = len(yy)
        print("[prep] %-5s %s" % (name, "  ".join(
            "%s %6d (%5.2f%%)" % (CLASSES[i], cnt.get(i, 0),
                                  100.0 * cnt.get(i, 0) / tot)
            for i in range(len(CLASSES)))))
    print("\n[prep] wrote %s and %s" % (OUT_NPZ, OUT_JSON))
    print("[prep] NOTE: the N class dominates. Report per-class sensitivity and")
    print("[prep]       positive predictivity for S and V, not overall accuracy --")
    print("[prep]       predicting 'N' for everything already scores ~89%.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
