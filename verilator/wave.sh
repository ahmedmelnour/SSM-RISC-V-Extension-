#!/bin/bash
# Waveform capture for GTKWave. Separate build dir, so the fast measurement
# path in obj_dir is never rebuilt or slowed down by tracing.
#
#   ./wave.sh [program] [trace_start_cycle] [trace_cycles]
#
# Tracing costs roughly 5-10x simulation speed, so dump a window, not a run.
set -e
cd "$(dirname "$0")"
PROG="${1:-ssmbench}"
START="${2:-150000}"
LEN="${3:-2000}"
export PATH=$HOME/tools/xpack-riscv-none-elf-gcc-13.2.0-2/bin:$PATH
R=..

echo "=== 1. firmware: $PROG ==="
( cd ../fw && OPT="-O2 -DSSM_QUICK" ./build.sh "$PROG" 2>&1 | grep -E "^\[fw\] [0-9]" )

echo "=== 2. traced simulator (separate obj_dir_trace) ==="
if [ ! -x obj_dir_trace/sim ]; then
  /home/ahmedelnour/verilator5/bin/verilator --cc --exe --build -j 8 \
    --MAKEFLAGS OPT_FAST=-O2 --trace -Wno-fatal -Wno-lint -Wno-style \
    --top-module a7lite_soc_top --Mdir obj_dir_trace \
    -I$R/vendor/cv32e40x/rtl/include -I$R/build -f core.f \
    $R/rtl/uart_tx.sv $R/vendor/cv32e40x/bhv/cv32e40x_sim_clock_gate.sv \
    $R/rtl/a7lite_soc_top.sv tb_main.cpp -o sim > /dev/null
else
  echo "  already built (rm -rf obj_dir_trace to force)"
fi

echo "=== 3. simulating, dumping cycles $START..$((START+LEN)) ==="
./obj_dir_trace/sim 600000000 "$START" "$LEN" > /dev/null

ls -lh wave.vcd
echo
echo "Open it:   gtkwave $(pwd)/wave.vcd &"
