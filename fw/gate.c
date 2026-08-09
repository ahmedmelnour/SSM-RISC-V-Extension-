/* ---------------------------------------------------------------------------
 * gate.c -- the week-2 gate: does the quantized SSM run *correctly* on the core?
 *
 * Runs the golden beats exported by py/export_c.py through ssm_model.c on the
 * CV32E40X and compares the resulting logits against the integers that
 * py/quantize.py produced for the same inputs on the host. The comparison is
 * exact: both sides are integer arithmetic, so there is no rounding budget and
 * anything other than equality is a failure.
 *
 * The firmware does the comparing itself and prints a verdict. The alternative
 * -- dumping logits over UART and eyeballing them -- is how a wrong answer gets
 * mistaken for a right one, and the expected values are already known at build
 * time, so there is no reason to move that judgement off the board.
 *
 * Also reports cycles and instructions for one inference, which is the first
 * real measurement of the actual model (STATUS §6 measured `ssm_scan_q15`, a
 * synthetic proxy with no projections -- see §7.3).
 *
 * Output is the same '#'-prefixed machine-parseable format bench.c uses.
 * --------------------------------------------------------------------------- */

#include <stdint.h>
#include "lib/io.h"
#include "lib/perf.h"
#include "ssm_model.h"
#include "model_data.h"
#include "golden.h"

static int32_t logits[M_CLASSES];

static void put_i32(int32_t v)
{
    if (v < 0) { uart_putc('-'); v = -v; }
    uart_put_u32((uint32_t)v);
}

/* Wrapped for perf_measure, which takes a void(*)(void*). */
static void run_one(void *arg)
{
    ssm_infer((const int16_t *)arg, logits);
}

int main(void)
{
    perf_init();

    uart_puts("\n#PERF,1\n");
    uart_puts("#CFG,program,gate\n");
    uart_puts("#CFG,d_model,");    uart_put_u32(M_D);       uart_puts("\n");
    uart_puts("#CFG,d_state,");    uart_put_u32(M_N);       uart_puts("\n");
    uart_puts("#CFG,layers,");     uart_put_u32(M_L);       uart_puts("\n");
    uart_puts("#CFG,timesteps,");  uart_put_u32(M_T);       uart_puts("\n");
    uart_puts("#CFG,beats,");      uart_put_u32(G_BEATS);   uart_puts("\n");
    /* Echoed back so a run can never be silently invalid: CV32E40X resets
     * mcountinhibit to "all inhibited" and the counters read frozen. */
    uart_puts("#CFG,mcountinhibit,");
    uart_put_hex32(perf_rd_mcountinhibit());
    uart_puts("\n");

    uart_puts("#COLS,beat,label,expect,got,exact\n");

    unsigned int bad = 0, wrongclass = 0;

    for (unsigned int b = 0; b < G_BEATS; b++) {
        ssm_infer(&g_x[b * M_T], logits);

        unsigned int mismatch = 0;
        for (int c = 0; c < M_CLASSES; c++) {
            if (logits[c] != g_logits[b * M_CLASSES + c]) mismatch = 1;
        }

        int pred = ssm_argmax(logits);
        int gold = ssm_argmax(&g_logits[b * M_CLASSES]);
        if (pred != gold) wrongclass++;
        if (mismatch) bad++;

        uart_puts("#DATA,");
        uart_put_u32(b);            uart_puts(",");
        uart_put_u32(g_label[b]);   uart_puts(",");
        uart_put_u32((uint32_t)gold); uart_puts(",");
        uart_put_u32((uint32_t)pred); uart_puts(",");
        uart_puts(mismatch ? "0" : "1");
        uart_puts("\n");

        /* On a mismatch, print both vectors -- a bare FAIL is not debuggable,
         * and this is the one run where the numbers are worth the UART time. */
        if (mismatch) {
            uart_puts("#DIFF,");
            uart_put_u32(b);
            for (int c = 0; c < M_CLASSES; c++) {
                uart_puts(",");
                put_i32(logits[c]);
                uart_puts(":");
                put_i32(g_logits[b * M_CLASSES + c]);
            }
            uart_puts("\n");
        }
    }

    /* One inference, measured. */
    perf_sample_t s = perf_measure(run_one, (void *)&g_x[0]);
    uart_puts("#PERF_CYCLES,");  uart_put_u64(s.cycles);  uart_puts("\n");
    uart_puts("#PERF_INSTRET,"); uart_put_u64(s.instret); uart_puts("\n");

    uart_puts("#RESULT,exact,");
    uart_put_u32(G_BEATS - bad);
    uart_puts(",of,");
    uart_put_u32(G_BEATS);
    uart_puts("\n");
    uart_puts("#RESULT,class_disagreements,");
    uart_put_u32(wrongclass);
    uart_puts("\n");
    uart_puts(bad ? "#GATE,FAIL\n" : "#GATE,PASS\n");
    uart_puts("#END\n");
    uart_flush();

    for (;;) {
        led_set(bad ? 0 : 1);
    }
    return 0;
}
