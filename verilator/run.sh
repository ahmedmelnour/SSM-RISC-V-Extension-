#!/bin/bash
# Run the SSM on the simulated CV32E40X soft core.
#   ./run.sh bench   perf counters per kernel   (~90 s)
#   ./run.sh gate    full SSM + golden check    (~13 min)
set -e
cd "$(dirname "$0")"
PROG="${1:-bench}"
CYC="${2:-600000000}"
export PATH=$HOME/tools/xpack-riscv-none-elf-gcc-13.2.0-2/bin:$PATH

echo "=== 1. building firmware: $PROG ==="
( cd ../fw && ./build.sh "$PROG" 2>&1 | grep -E "^\[fw\]|text" )

echo
echo "=== 2. building the simulator (Verilator 5.052) ==="
[ -x obj_dir/sim ] && echo "  already built (delete obj_dir to force a rebuild)" || ./build.sh

echo
echo "=== 3. simulating CV32E40X RTL, up to $CYC cycles ==="
time ./obj_dir/sim "$CYC" | tee "results/${PROG}_run.log"

echo
echo "=== 4. summary ==="
python3 summarise.py "results/${PROG}_run.log"
