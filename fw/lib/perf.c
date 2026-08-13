/* ---------------------------------------------------------------------------
 * perf.c -- implementation of the measurement harness declared in perf.h.
 *
 * Everything here exists to make one claim defensible: that a reported cycle
 * count is the kernel's, and not the harness's.
 *
 * TWO RULES THIS FILE IS BUILT AROUND
 * -----------------------------------
 * 1. Calibrate through the *exact* path you measure through.
 *    Both perf_init() and perf_measure*() call the same raw_measure(). If
 *    calibration went through a lighter path -- inlined CSR reads, no function
 *    pointer, no struct return -- the difference would be a SYSTEMATIC bias, and
 *    systematic biases do not average out. They just move every number by a
 *    constant you never see.
 *
 * 2. The event floor is per-event, not one constant.
 *    With PERF_EV_INSTRET selected, mhpmcounter3 counts the harness's own CSR
 *    reads (~20 instructions). With PERF_EV_DATA_WRITE selected it counts almost
 *    nothing. A single global floor would over-correct one and under-correct the
 *    other. So all 16 are calibrated separately and emitted as #OVH lines, so a
 *    reader can audit the correction instead of trusting it.
 *
 * Minimum, not mean: the floor is the cost when nothing goes wrong, and nothing
 * can make the harness *faster* than its own instruction sequence. Averaging
 * would fold in any one-off slow trial and over-subtract.
 * --------------------------------------------------------------------------- */

#include "lib/perf.h"

/* Trials per calibration point. The kernels here are deterministic (no cache,
 * no branch predictor, no interrupts), so this converges immediately -- 8 is
 * cheap insurance against a first-call outlier, not a statistical sample. */
#define PERF_CAL_TRIALS 8

/* Index order must match perf_event_t exactly: bench.c indexes this array with
 * the event id straight out of a perf_sample_t. */
const char *const perf_event_names[PERF_EV_COUNT] = {
    "cycle",          /*  0 */
    "instret",        /*  1 */
    "compressed",     /*  2 */
    "jump",           /*  3 */
    "branch",         /*  4 */
    "branch_taken",   /*  5 */
    "intr_taken",     /*  6 */
    "data_read",      /*  7 */
    "data_write",     /*  8 */
    "if_invalid",     /*  9 */
    "id_invalid",     /* 10 */
    "ex_invalid",     /* 11 */
    "wb_invalid",     /* 12 */
    "ld_stall",       /* 13 */
    "jalr_stall",     /* 14 */
    "wb_data_stall"   /* 15 */
};

uint32_t perf_overhead_cycles;
uint32_t perf_overhead_instret;
uint32_t perf_overhead_event[PERF_EV_COUNT];

/* ---------------------------------------------------------------------------
 * The one measurement path.
 *
 * Called through a function pointer so the compiler cannot inline the kernel
 * into the timed region and change its instruction mix between the calibration
 * call and the real one. The struct return (32 bytes, so RV32 returns it via a
 * hidden pointer) is likewise part of the measured path on both sides, which is
 * exactly why it does not matter that it costs something.
 *
 * Read order is deliberately symmetric -- event, instret, cycles going in;
 * cycles, instret, event coming out -- so mcycle brackets the call as tightly
 * as possible and the two outer reads contribute equally to both brackets.
 * --------------------------------------------------------------------------- */
static perf_sample_t raw_measure(void (*fn)(void *), void *arg)
{
    perf_sample_t s;
    uint64_t c0, i0, e0, c1, i1, e1;

    e0 = perf_event_count();
    i0 = perf_instret();
    c0 = perf_cycles();

    fn(arg);

    c1 = perf_cycles();
    i1 = perf_instret();
    e1 = perf_event_count();

    s.cycles   = c1 - c0;
    s.instret  = i1 - i0;
    s.event    = e1 - e0;
    s.event_id = PERF_EV_CYCLE;     /* callers that care overwrite this */
    return s;
}

/* Subtract the calibrated floor, saturating at zero.
 *
 * Saturation matters: an empty kernel can measure fractionally below its own
 * calibration floor, and an unsigned wrap would report it as ~1.8e19 cycles.
 * That is a much more alarming-looking number than the 0 it should be. */
static uint64_t sat_sub(uint64_t v, uint32_t floor)
{
    return (v > (uint64_t)floor) ? (v - (uint64_t)floor) : (uint64_t)0;
}

/* The kernel calibration runs against: a call that does nothing, so whatever it
 * measures is the harness. Not static-inline, and reached through the same
 * function pointer as a real kernel. */
static void perf_nop_kernel(void *arg)
{
    (void)arg;
}

void perf_init(void)
{
    int ev, i;

    /* THE ONE THING THAT WILL WASTE YOUR AFTERNOON (see perf.h).
     * CV32E40X resets mcountinhibit with every implemented counter DISABLED, to
     * save power. Until this write lands, mcycle/minstret/mhpmcounter3 read back
     * a frozen constant and every kernel measures 0 cycles -- which looks like a
     * broken harness, not a disabled counter. bench.c echoes the register back
     * as #CFG,mcountinhibit,0 precisely so this failure is visible in the data
     * rather than inferred from it. */
    perf_wr_mcountinhibit(0u);

    /* Cycle and instruction floors. The selected event is irrelevant here --
     * mcycle and minstret are separate counters -- but something has to be
     * selected, so pick the one whose meaning is fixed. */
    perf_select_event(PERF_EV_CYCLE);

    perf_overhead_cycles  = 0xFFFFFFFFu;
    perf_overhead_instret = 0xFFFFFFFFu;

    for (i = 0; i < PERF_CAL_TRIALS; i++) {
        perf_sample_t s = raw_measure(perf_nop_kernel, 0);

        if ((uint32_t)s.cycles  < perf_overhead_cycles)
            perf_overhead_cycles  = (uint32_t)s.cycles;
        if ((uint32_t)s.instret < perf_overhead_instret)
            perf_overhead_instret = (uint32_t)s.instret;
    }

    /* Per-event floors. Rule 2 above: this loop is the whole reason
     * perf_overhead_event is an array and not a scalar. */
    for (ev = 0; ev < PERF_EV_COUNT; ev++) {
        uint32_t best = 0xFFFFFFFFu;

        perf_select_event((perf_event_t)ev);

        for (i = 0; i < PERF_CAL_TRIALS; i++) {
            perf_sample_t s = raw_measure(perf_nop_kernel, 0);
            if ((uint32_t)s.event < best)
                best = (uint32_t)s.event;
        }

        perf_overhead_event[ev] = best;
    }
}

perf_sample_t perf_measure(void (*fn)(void *), void *arg)
{
    perf_sample_t s = raw_measure(fn, arg);

    s.cycles  = sat_sub(s.cycles,  perf_overhead_cycles);
    s.instret = sat_sub(s.instret, perf_overhead_instret);

    /* Documented as not filled in: whatever event happens to be selected is
     * whatever the last perf_measure_event() left behind, so reporting it would
     * be a value with no stated meaning. */
    s.event    = 0;
    s.event_id = PERF_EV_CYCLE;
    return s;
}

perf_sample_t perf_measure_event(void (*fn)(void *), void *arg, perf_event_t ev)
{
    perf_sample_t s;

    /* ev indexes perf_overhead_event[] and, in bench.c, perf_event_names[].
     * Clamp rather than trust the caller -- an out-of-range event would read off
     * the end of both arrays and produce a plausible-looking wrong row. */
    if ((unsigned)ev >= (unsigned)PERF_EV_COUNT)
        ev = PERF_EV_CYCLE;

    perf_select_event(ev);

    s = raw_measure(fn, arg);

    s.cycles   = sat_sub(s.cycles,  perf_overhead_cycles);
    s.instret  = sat_sub(s.instret, perf_overhead_instret);
    s.event    = sat_sub(s.event,   perf_overhead_event[ev]);
    s.event_id = ev;
    return s;
}
