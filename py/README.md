# `py/` — model development

Everything that runs on a workstation rather than on the core: dataset fetch,
preprocessing, training, quantization, and the fixed-point reference that the C
port must match bit-for-bit.

Nothing in here is part of the FPGA build. The only artefact that crosses over is
the exported weight header consumed by `fw/`.

## Setup

```bash
py/venv/bin/python py/fetch_data.py     # MIT-BIH -> py/data/mitdb (resumable)
py/venv/bin/python py/prep_data.py      # -> py/data/beats.npz + beats.json
```

`py/venv/` and `py/data/` are gitignored — the venv is bootstrapped and the
dataset is re-downloadable from PhysioNet.

### Recreating the venv

This machine's Python 3.10 has **no `ensurepip`**, so the obvious command fails:

```
$ python3 -m venv py/venv
The virtual environment was not created successfully because ensurepip is not
available. On Debian/Ubuntu systems, you need to install the python3-venv package
```

`apt install python3.10-venv` needs root. Without it, bootstrap pip *inside* the
venv instead — this keeps everything under `py/` and touches nothing system-wide:

```bash
python3 -m venv --without-pip py/venv
curl -sSO https://bootstrap.pypa.io/pip/get-pip.py
py/venv/bin/python get-pip.py
py/venv/bin/pip install wfdb scikit-learn matplotlib numpy scipy torch
```

`torch` comes from PyPI, which on Linux means the CUDA build and its `nvidia-*`
dependencies (~2-3 GB). The slim CPU wheel lives on `download.pytorch.org`; it is
the better choice for a model this size if that index is reachable. Either runs
fine on CPU — the model is small enough that training is a few minutes either way.

The `torch` wheel is large enough that the download sometimes drops partway.
`pip install --resume-retries 5 torch` picks up where it left off.

## Dataset notes

`prep_data.py` header documents the choices in full. The three that most affect
whether the numbers are defensible:

- **Inter-patient DS1/DS2 split** (de Chazal 2004), not a random beat split.
  A random split reports ~99% accuracy that does not survive a new patient.
- **Paced records 102/104/107/217 excluded**, per AAMI EC57.
- **MLII selected by name** — it is channel 1 in record 114, channel 0 elsewhere.

The class distribution is heavily skewed to normal beats, so overall accuracy is
close to meaningless: always predicting `N` already scores about 89%. Report
per-class sensitivity and positive predictivity for `S` and `V`.

## Fixed-point ordering

The C port is validated against the **fixed-point Python reference**, not against
the float model. Quantized-vs-float accuracy is reported separately. Getting this
ordering backwards is what makes a "bit-exact" claim vacuous — see STATUS.md §8.7.
