// Verilator testbench for a7lite_soc_top: runs the firmware and decodes UART.
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include "Va7lite_soc_top.h"
#include "verilated.h"
#if VM_TRACE
#include "verilated_vcd_c.h"
#endif

static const int CLK_HZ = 50000000, BAUD = 115200;
static const int CPB    = CLK_HZ / BAUD;      // clocks per bit = 434

int main(int argc, char** argv) {
    Verilated::commandArgs(argc, argv);
    auto* top = new Va7lite_soc_top;
    uint64_t max_cycles = (argc > 1) ? strtoull(argv[1], nullptr, 0) : 400000000ULL;

#if VM_TRACE
    // Waveform capture over a WINDOW, not the whole run: the full netlist at
    // ~800k cycles/s would produce a VCD measured in gigabytes, and the first
    // hundred thousand cycles are boot and .bss zeroing, not the kernel.
    //   argv[2] = first cycle to dump, argv[3] = how many cycles
    uint64_t tr_start = (argc > 2) ? strtoull(argv[2], nullptr, 0) : 0ULL;
    uint64_t tr_len   = (argc > 3) ? strtoull(argv[3], nullptr, 0) : 20000ULL;
    Verilated::traceEverOn(true);
    auto* tfp = new VerilatedVcdC;
    top->trace(tfp, 4);                 // depth 4 -- SoC + core top levels
    tfp->open("wave.vcd");
    bool tracing = false;
#endif

    // UART RX state
    int  st = 0, bit_i = 0, cnt = 0; uint8_t ch = 0; int prev = 1;
    // stop as soon as the firmware says it is finished, so the run takes only
    // as long as the workload rather than the full cycle budget
    const char* END = "#END"; int em = 0; uint64_t last_char = 0;

    for (uint64_t c = 0; c < max_cycles; ++c) {
        top->clk_i = 0; top->eval();
#if VM_TRACE
        if (c >= tr_start && c < tr_start + tr_len) { tracing = true; tfp->dump(2 * c); }
        else if (tracing) { tracing = false; tfp->close(); }
#endif
        top->clk_i = 1; top->eval();
#if VM_TRACE
        if (tracing) tfp->dump(2 * c + 1);
#endif

        int tx = top->uart_tx_o;
        if (st == 0) {                       // idle: wait for start bit
            if (prev == 1 && tx == 0) { st = 1; cnt = CPB + CPB/2; bit_i = 0; ch = 0; }
        } else {
            if (--cnt <= 0) {
                if (bit_i < 8) { ch |= (tx & 1) << bit_i; ++bit_i; cnt = CPB; }
                else {
                    putchar(ch); fflush(stdout); st = 0; last_char = c;
                    em = (ch == END[em]) ? em + 1 : (ch == END[0]);
                    if (END[em] == '\0') { putchar('\n'); break; }
                }
            }
        }
        prev = tx;
        // also stop if the firmware has gone quiet for a long while
        if (last_char && c - last_char > 40000000ULL) break;
    }
#if VM_TRACE
    if (tracing) tfp->close();
    fprintf(stderr, "\n[trace] wave.vcd written for cycles %llu..%llu\n",
            (unsigned long long)tr_start, (unsigned long long)(tr_start + tr_len));
#endif
    top->final(); delete top;
    return 0;
}
