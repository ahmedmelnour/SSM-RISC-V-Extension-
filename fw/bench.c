/* ---------------------------------------------------------------------------
 * bench.c -- measurement harness.
 *
 * Emits machine-parseable lines over UART; scripts/run_bench.py consumes them.
 * Format (every harness line starts with '#' so stray text cannot be mistaken
 * for data):
 *
 *   #PERF,1                                   protocol version
 *   #CFG,key,value                            clock, overheads, build info
 *   #COLS,...                                 column names for #DATA
 *   #DATA,kernel,reps,cycles,instret,ev_id,ev_name,ev_count,checksum
 *   #END
 *
 * Each kernel is run once per event of interest, because the SoC is built with
 * NUM_MHPMCOUNTERS = 1 (one selectable event counter). The kernels are
 * deterministic, so re-running to collect a different event is sound -- there
 * is no cache or branch predictor whose state would differ between runs.
 *
 * Working sets live in .bss and are filled by a deterministic LCG at startup,
 * rather than being static initialised data. That keeps them out of the BRAM
 * image: .bss is NOLOAD and zeroed by crt0, so 4 KB of buffers costs nothing
 * in the bitstream.
 *
 * Every kernel folds its result into `checksum`, which is volatile and printed.
 * Without that, -O2 is entitled to delete the whole kernel as dead code and you
 * would be timing an empty loop.
 * --------------------------------------------------------------------------- */

#include <stdint.h>
#include "lib/io.h"
#include "lib/perf.h"

/* ---- problem sizes ---- */
#define SSM_T   32          /* timesteps            */
#define SSM_N   16          /* state dimension      */
#define GEMM_M  8
#define GEMM_K  8
#define GEMM_N  8
#define DOT_LEN 256

/* ---- working sets (.bss) ---- */
static int16_t ssm_a[SSM_T][SSM_N];
static int16_t ssm_b[SSM_T][SSM_N];
static int16_t ssm_c[SSM_T][SSM_N];
static int16_t ssm_x[SSM_T];
static int32_t ssm_h[SSM_N];
static int32_t ssm_y[SSM_T];

static int8_t  gemm_a[GEMM_M][GEMM_K];
static int8_t  gemm_b[GEMM_K][GEMM_N];
static int32_t gemm_c[GEMM_M][GEMM_N];

static int8_t  dot_u[DOT_LEN];
static int8_t  dot_v[DOT_LEN];

volatile int32_t checksum;

/* ---- deterministic filler ---- */
static uint32_t rng_state = 0x12345678u;

static uint32_t rnd(void)
{
    rng_state = rng_state * 1664525u + 1013904223u;
    return rng_state;
}

static void fill_inputs(void)
{
    for (int t = 0; t < SSM_T; t++) {
        for (int n = 0; n < SSM_N; n++) {
            /* Q15 coefficients kept well inside range so the scan cannot
             * saturate and change the instruction mix between runs. */
            ssm_a[t][n] = (int16_t)((rnd() % 24576u) + 4096u);   /* 0.125..0.875 */
            ssm_b[t][n] = (int16_t)((rnd() % 16384u) - 8192);
            ssm_c[t][n] = (int16_t)((rnd() % 16384u) - 8192);
        }
        ssm_x[t] = (int16_t)((rnd() % 16384u) - 8192);
    }
    for (int i = 0; i < GEMM_M; i++)
        for (int j = 0; j < GEMM_K; j++)
            gemm_a[i][j] = (int8_t)(rnd() % 256u);
    for (int i = 0; i < GEMM_K; i++)
        for (int j = 0; j < GEMM_N; j++)
            gemm_b[i][j] = (int8_t)(rnd() % 256u);
    for (int i = 0; i < DOT_LEN; i++) {
        dot_u[i] = (int8_t)(rnd() % 256u);
        dot_v[i] = (int8_t)(rnd() % 256u);
    }
}

/* ---------------------------------------------------------------------------
 * Kernels
 * --------------------------------------------------------------------------- */

/* Harness floor: measures the cost of the call plus the perf read sequence. */
static void k_nop(void *arg)
{
    (void)arg;
}

/* Quantized selective state-space scan -- the FYP's target kernel shape.
 *   h[n] = (a[t][n]*h[n] + b[t][n]*x[t]) >> 15
 *   y[t] = sum_n c[t][n]*h[n] >> 15
 * Coefficients vary per timestep, which is what makes the scan "selective"
 * and what prevents hoisting them out of the loop. */
static void k_ssm_scan_q15(void *arg)
{
    (void)arg;
    int32_t acc_sum = 0;

    for (int n = 0; n < SSM_N; n++) ssm_h[n] = 0;

    for (int t = 0; t < SSM_T; t++) {
        int32_t xt  = ssm_x[t];
        int32_t acc = 0;
        for (int n = 0; n < SSM_N; n++) {
            int32_t h = ssm_h[n];
            h = (int32_t)(((int32_t)ssm_a[t][n] * h) >> 15)
              + (int32_t)(((int32_t)ssm_b[t][n] * xt) >> 15);
            ssm_h[n] = h;
            acc += ((int32_t)ssm_c[t][n] * h) >> 15;
        }
        ssm_y[t] = acc;
        acc_sum += acc;
    }
    checksum += acc_sum;
}

/* INT8 GEMM -- the declared fallback kernel if the SSM path stalls. */
static void k_gemm_i8(void *arg)
{
    (void)arg;
    int32_t s = 0;

    for (int i = 0; i < GEMM_M; i++) {
        for (int j = 0; j < GEMM_N; j++) {
            int32_t acc = 0;
            for (int k = 0; k < GEMM_K; k++) {
                acc += (int32_t)gemm_a[i][k] * (int32_t)gemm_b[k][j];
            }
            gemm_c[i][j] = acc;
            s += acc;
        }
    }
    checksum += s;
}

/* INT8 dot product -- simplest MAC-bound loop, useful as a roofline anchor. */
static void k_dot_i8(void *arg)
{
    (void)arg;
    int32_t acc = 0;
    for (int i = 0; i < DOT_LEN; i++) {
        acc += (int32_t)dot_u[i] * (int32_t)dot_v[i];
    }
    checksum += acc;
}

/* Pure load/store loop -- isolates memory behaviour from arithmetic. */
static void k_memcpy32(void *arg)
{
    (void)arg;
    int32_t s = 0;
    for (int t = 0; t < SSM_T; t++) {
        ssm_y[t] = (int32_t)ssm_x[t];
        s += ssm_y[t];
    }
    checksum += s;
}

typedef struct {
    const char *name;
    void      (*fn)(void *);
    uint32_t    reps;   /* inner repeats folded into one measurement */
} kernel_t;

static const kernel_t kernels[] = {
    { "nop",           k_nop,          1 },
    { "dot_i8",        k_dot_i8,       1 },
    { "gemm_i8",       k_gemm_i8,      1 },
    { "ssm_scan_q15",  k_ssm_scan_q15, 1 },
    { "memcpy32",      k_memcpy32,     1 },
};
#define NUM_KERNELS ((int)(sizeof(kernels) / sizeof(kernels[0])))

/* Events swept per kernel. Deliberately not all 16: these are the ones that
 * bear on an accelerator argument -- memory traffic, stalls, control flow. */
static const perf_event_t sweep[] = {
    PERF_EV_INSTRET,
    PERF_EV_COMPRESSED,
    PERF_EV_BRANCH,
    PERF_EV_BRANCH_TAKEN,
    PERF_EV_DATA_READ,
    PERF_EV_DATA_WRITE,
    PERF_EV_LD_STALL,
    PERF_EV_WB_DATA_STALL,
};
#define NUM_SWEEP ((int)(sizeof(sweep) / sizeof(sweep[0])))

static void emit_cfg(const char *k, uint64_t v)
{
    uart_puts("#CFG,");
    uart_puts(k);
    uart_putc(',');
    uart_put_u64(v);
    uart_puts("\n");
}

int main(void)
{
    perf_init();
    fill_inputs();

    uart_puts("\n#PERF,1\n");
    emit_cfg("clk_hz", 50000000u);
    emit_cfg("overhead_cycles", perf_overhead_cycles);
    emit_cfg("overhead_instret", perf_overhead_instret);
    emit_cfg("mcountinhibit", perf_rd_mcountinhibit());
    emit_cfg("num_kernels", NUM_KERNELS);
    emit_cfg("num_events", NUM_SWEEP);

    /* Per-event calibration floors, so a reader can audit the correction
     * rather than having to trust it. */
    for (int ei = 0; ei < NUM_SWEEP; ei++) {
        uart_puts("#OVH,");
        uart_puts(perf_event_names[sweep[ei]]);
        uart_putc(',');
        uart_put_u32(perf_overhead_event[sweep[ei]]);
        uart_puts("\n");
    }

    uart_puts("#COLS,kernel,reps,cycles,instret,ev_id,ev_name,ev_count,checksum\n");

    for (int ki = 0; ki < NUM_KERNELS; ki++) {
        for (int ei = 0; ei < NUM_SWEEP; ei++) {
            led_set(ei & 1);

            perf_sample_t s = perf_measure_event(kernels[ki].fn, 0, sweep[ei]);

            uart_puts("#DATA,");
            uart_puts(kernels[ki].name);
            uart_putc(',');
            uart_put_u32(kernels[ki].reps);
            uart_putc(',');
            uart_put_u64(s.cycles);
            uart_putc(',');
            uart_put_u64(s.instret);
            uart_putc(',');
            uart_put_u32((uint32_t)s.event_id);
            uart_putc(',');
            uart_puts(perf_event_names[s.event_id]);
            uart_putc(',');
            uart_put_u64(s.event);
            uart_putc(',');
            uart_put_hex32((uint32_t)checksum);
            uart_puts("\n");
        }
    }

    uart_puts("#END\n");
    uart_flush();

    /* Idle visibly rather than falling off the end of main(). */
    for (;;) {
        led_set(1);
        for (volatile int i = 0; i < 200000; i++) { }
        led_set(0);
        for (volatile int i = 0; i < 200000; i++) { }
    }
    return 0;
}
