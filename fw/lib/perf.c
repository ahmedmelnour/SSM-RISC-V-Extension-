/* ---------------------------------------------------------------------------
 * perf.c -- see perf.h for the counter map and the mcountinhibit gotcha.
 *
 * Calibration principle: the overhead floor must be measured through the EXACT
 * code path used for real measurements. An earlier version calibrated cycles
 * and instructions through a lighter path than the one that also reads the
 * event counter, which left every event count biased high by a constant ~20
 * instructions (the CSR reads inside the event window) and left the `nop`
 * kernel reading 4 cycles instead of ~0.
 *
 * So there is now exactly one measurement path, raw_measure(), and everything
 * -- including perf_measure() -- goes through it.
 * --------------------------------------------------------------------------- */

#include "perf.h"

const char *const perf_event_names[PERF_EV_COUNT] = {
    "cycle",     "instret",    "compressed", "jump",
    "branch",    "branch_tkn", "intr_taken", "data_read",
    "data_write","if_invalid", "id_invalid", "ex_invalid",
    "wb_invalid","ld_stall",   "jalr_stall", "wb_data_stall"
};

uint32_t perf_overhead_cycles  = 0;
uint32_t perf_overhead_instret = 0;
uint32_t perf_overhead_event[PERF_EV_COUNT];

static void perf_nop_kernel(void *arg)
{
    (void)arg;
}

/* The single measurement path. The event counter is read innermost-last so its
 * window is as tight as the cycle/instret windows; whatever residual remains is
 * identical for the calibration run and is therefore subtracted exactly. */
static void raw_measure(void (*fn)(void *), void *arg, perf_event_t ev,
                        uint32_t *d_cyc, uint32_t *d_instr, uint32_t *d_ev)
{
    uint64_t e0 = perf_event_count();
    uint64_t c0 = perf_cycles();
    uint64_t i0 = perf_instret();

    fn(arg);

    uint64_t i1 = perf_instret();
    uint64_t c1 = perf_cycles();
    uint64_t e1 = perf_event_count();

    (void)ev;
    *d_cyc   = (uint32_t)(c1 - c0);
    *d_instr = (uint32_t)(i1 - i0);
    *d_ev    = (uint32_t)(e1 - e0);
}

static uint64_t sub_sat(uint64_t a, uint32_t b)
{
    return (a > (uint64_t)b) ? (a - (uint64_t)b) : 0u;
}

void perf_init(void)
{
    /* Clear every inhibit bit. Writes are masked by MCOUNTINHIBIT_MASK in
     * hardware, so writing 0 simply enables all implemented counters. */
    perf_wr_mcountinhibit(0u);

    uint32_t best_c = 0xFFFFFFFFu;
    uint32_t best_i = 0xFFFFFFFFu;

    /* Per-event floor: the event counter's own overhead depends on which event
     * is selected (e.g. `instret` counts the harness's CSR reads, `data_write`
     * counts almost nothing), so it cannot be a single global constant. */
    for (int ev = 0; ev < PERF_EV_COUNT; ev++) {
        perf_select_event((perf_event_t)ev);

        uint32_t best_e = 0xFFFFFFFFu;
        for (int trial = 0; trial < 8; trial++) {
            uint32_t dc, di, de;
            raw_measure(perf_nop_kernel, 0, (perf_event_t)ev, &dc, &di, &de);
            if (de < best_e) best_e = de;
            if (dc < best_c) best_c = dc;
            if (di < best_i) best_i = di;
        }
        perf_overhead_event[ev] = best_e;
    }

    perf_overhead_cycles  = best_c;
    perf_overhead_instret = best_i;

    perf_select_event(PERF_EV_INSTRET);
}

perf_sample_t perf_measure_event(void (*fn)(void *), void *arg, perf_event_t ev)
{
    perf_sample_t s;
    uint32_t dc, di, de;

    perf_select_event(ev);
    raw_measure(fn, arg, ev, &dc, &di, &de);

    s.cycles   = sub_sat(dc, perf_overhead_cycles);
    s.instret  = sub_sat(di, perf_overhead_instret);
    s.event    = sub_sat(de, perf_overhead_event[ev]);
    s.event_id = ev;
    return s;
}

perf_sample_t perf_measure(void (*fn)(void *), void *arg)
{
    /* Same path, so the same calibration applies. */
    return perf_measure_event(fn, arg, PERF_EV_CYCLE);
}
