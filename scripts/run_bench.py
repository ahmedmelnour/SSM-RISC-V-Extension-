#!/usr/bin/env python3
"""
run_bench.py -- one command from source to a parsed results file.

    ./scripts/run_bench.py                    # firmware + bitstream + program + capture
    ./scripts/run_bench.py --no-bit           # reuse the existing bitstream
    ./scripts/run_bench.py --capture-only     # board already programmed
    ./scripts/run_bench.py --tag baseline     # label the results file

Why this exists as a script rather than a checklist: the firmware image is baked
into the BRAM initialisation, so *any* firmware change requires a full Vivado
rebuild. Doing that by hand is where stale-bitstream mistakes come from -- you
change a kernel, forget to re-run synthesis, and measure the old code.

Output: results/<tag>_<timestamp>.csv plus a .meta file recording the git
revision, compiler flags, and utilisation, so a number can always be traced
back to what produced it.

Capture note: the serial reader starts BEFORE programming, because the harness
prints immediately out of reset and would otherwise be missed. It reads until
'#END' rather than for a fixed window.
"""

import argparse
import os
import subprocess
import sys
import termios
import threading
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIVADO = "/opt/Xilinx/2026.1/Vivado/bin/vivado"

BAUD_CONST = {9600: termios.B9600, 115200: termios.B115200}


# ---------------------------------------------------------------------------
# serial
# ---------------------------------------------------------------------------
class SerialReader(threading.Thread):
    """Reads the port in the background until '#END' or stop() is called."""

    def __init__(self, port, baud):
        super().__init__(daemon=True)
        self.buf = bytearray()
        self._stop = threading.Event()
        self.done = threading.Event()
        self.fd = os.open(port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)

        attrs = termios.tcgetattr(self.fd)
        iflag, oflag, cflag, lflag, _, _, cc = attrs
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        iflag = termios.IGNPAR
        oflag = 0
        lflag = 0
        cc = list(cc)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        speed = BAUD_CONST[baud]
        termios.tcsetattr(self.fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])
        termios.tcflush(self.fd, termios.TCIFLUSH)

    def run(self):
        while not self._stop.is_set():
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                chunk = b""
            if chunk:
                self.buf += chunk
                if b"#END" in self.buf:
                    self.done.set()
                    break
            else:
                time.sleep(0.01)
        os.close(self.fd)

    def stop(self):
        self._stop.set()

    def text(self):
        return self.buf.decode("ascii", errors="replace")


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------
def run(cmd, desc, cwd=ROOT):
    print("[run_bench] %s" % desc)
    r = subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str),
                       capture_output=True, text=True)
    if r.returncode != 0:
        sys.stderr.write(r.stdout[-4000:] + "\n" + r.stderr[-4000:] + "\n")
        raise SystemExit("[run_bench] FAILED: %s (exit %d)" % (desc, r.returncode))
    return r.stdout


def build_firmware(prog, opt):
    env = "OPT=%s " % opt if opt else ""
    return run("%s./fw/build.sh %s" % (env, prog), "building firmware (%s)" % prog)


def build_bitstream():
    return run([VIVADO, "-mode", "batch", "-nojournal", "-nolog",
                "-source", "scripts/build.tcl"], "synthesis + implementation (slow)")


def program():
    return run([VIVADO, "-mode", "batch", "-nojournal", "-nolog",
                "-source", "scripts/program.tcl"], "programming board")


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------
def parse(text):
    cfg, rows, cols = {}, [], []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#"):
            continue
        parts = line.split(",")
        tag = parts[0]
        if tag == "#CFG" and len(parts) >= 3:
            cfg[parts[1]] = parts[2]
        elif tag == "#COLS":
            cols = parts[1:]
        elif tag == "#DATA":
            rows.append(parts[1:])
    return cfg, cols, rows


def summarise(cfg, cols, rows):
    """Print a human-readable table; cycles/instret are identical across the
    event sweep for a given kernel, so collapse them and show events wide."""
    try:
        i_k = cols.index("kernel")
        i_c = cols.index("cycles")
        i_i = cols.index("instret")
        i_en = cols.index("ev_name")
        i_ev = cols.index("ev_count")
    except ValueError:
        print("[run_bench] unexpected column layout: %s" % cols)
        return

    order, per = [], {}
    for r in rows:
        k = r[i_k]
        if k not in per:
            per[k] = {"cycles": int(r[i_c]), "instret": int(r[i_i]), "events": {}}
            order.append(k)
        per[k]["events"][r[i_en]] = int(r[i_ev])

    ev_names = []
    for k in order:
        for e in per[k]["events"]:
            if e not in ev_names:
                ev_names.append(e)

    hdr = "%-14s %10s %10s %7s" % ("kernel", "cycles", "instret", "IPC")
    for e in ev_names:
        hdr += " %13s" % e
    print("\n" + hdr)
    print("-" * len(hdr))
    for k in order:
        d = per[k]
        ipc = (d["instret"] / d["cycles"]) if d["cycles"] else 0.0
        line = "%-14s %10d %10d %7.3f" % (k, d["cycles"], d["instret"], ipc)
        for e in ev_names:
            line += " %13d" % d["events"].get(e, 0)
        print(line)
    print()

    clk = int(cfg.get("clk_hz", "50000000"))
    print("clock %d Hz, harness overhead %s cycles / %s instr"
          % (clk, cfg.get("overhead_cycles", "?"), cfg.get("overhead_instret", "?")))
    if cfg.get("mcountinhibit") not in (None, "0"):
        print("WARNING: mcountinhibit = %s (non-zero). Counters were INHIBITED; "
              "measurements are not valid." % cfg["mcountinhibit"])


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--prog", default="bench")
    ap.add_argument("--opt", default="-O2")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--no-fw", action="store_true", help="skip firmware build")
    ap.add_argument("--no-bit", action="store_true", help="skip Vivado build")
    ap.add_argument("--capture-only", action="store_true",
                    help="board is already programmed and running")
    args = ap.parse_args()

    outdir = os.path.join(ROOT, "results")
    os.makedirs(outdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(outdir, "%s_%s" % (args.tag, stamp))

    if not args.capture_only:
        if not args.no_fw:
            build_firmware(args.prog, args.opt)
        if not args.no_bit:
            build_bitstream()

    # Reader first: the harness prints immediately out of reset.
    print("[run_bench] opening %s at %d" % (args.port, args.baud))
    reader = SerialReader(args.port, args.baud)
    reader.start()
    time.sleep(0.3)

    if not args.capture_only:
        program()
    else:
        print("[run_bench] capture-only: waiting for a running board")

    print("[run_bench] waiting for #END (timeout %.0fs)" % args.timeout)
    got = reader.done.wait(timeout=args.timeout)
    reader.stop()
    time.sleep(0.2)

    text = reader.text()
    if not got:
        print("[run_bench] TIMEOUT: no #END seen. Captured %d bytes." % len(text))
        if text:
            print(text[-2000:])
        raise SystemExit(2)

    cfg, cols, rows = parse(text)
    if not rows:
        raise SystemExit("[run_bench] no #DATA rows parsed")

    with open(base + ".csv", "w") as f:
        f.write(",".join(cols) + "\n")
        for r in rows:
            f.write(",".join(r) + "\n")

    meta = []
    meta.append("timestamp=%s" % stamp)
    meta.append("tag=%s" % args.tag)
    for k, v in cfg.items():
        meta.append("cfg.%s=%s" % (k, v))
    try:
        meta.append("git_rev=%s" % subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            capture_output=True, text=True).stdout.strip())
        meta.append("git_dirty=%s" % bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT,
            capture_output=True, text=True).stdout.strip()))
    except Exception:
        pass
    bi = os.path.join(ROOT, "fw", "build_info.txt")
    if os.path.exists(bi):
        with open(bi) as f:
            meta += ["fw.%s" % l.strip() for l in f if l.strip()]
    util = os.path.join(ROOT, "build", "post_route_util.rpt")
    if os.path.exists(util):
        with open(util) as f:
            for line in f:
                for key in ("Slice LUTs", "Slice Registers", "Block RAM Tile", "DSPs"):
                    if line.strip().startswith("| " + key):
                        cells = [c.strip() for c in line.split("|")]
                        if len(cells) > 3:
                            meta.append("util.%s=%s" % (key.replace(" ", "_"), cells[2]))
    with open(base + ".meta", "w") as f:
        f.write("\n".join(meta) + "\n")

    with open(base + ".raw.txt", "w") as f:
        f.write(text)

    summarise(cfg, cols, rows)
    print("[run_bench] wrote %s.csv / .meta / .raw.txt" % base)
    return 0


if __name__ == "__main__":
    sys.exit(main())
