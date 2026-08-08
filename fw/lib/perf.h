/* ---------------------------------------------------------------------------
 * perf.h -- cycle and event measurement on CV32E40X.
 *
 * THE ONE THING THAT WILL WASTE YOUR AFTERNOON
 * --------------------------------------------
 * CV32E40X resets mcountinhibit to MCOUNTINHIBIT_MASK, i.e. every implemented
 * counter is DISABLED out of reset (cv32e40x_cs_registers.sv: "implemented
 * counters are disabled out of reset to save power"). Until software clears
 * mcountinhibit, mcycle and minstret read back a frozen constant -- which looks
 * exactly like a broken measurement harness. Call perf_init() once at startup.
 *
 * Counter map (from the RTL, not guessed):
 *   mcycle       0xB00 / 0xB80   always implemented, gated by mcountinhibit[0]
 *   minstret     0xB02 / 0xB82   always implemented, gated by mcountinhibit[2]
 *   mhpmcounter3 0xB03 / 0xB83   present when NUM_MHPMCOUNTERS >= 1
 *   mhpmevent3   0x323           EVENT BITMASK, not an event index
 *
 * mhpmevent3 is a mask: the counter increments in any cycle where
 *   |(hpm_events & mhpmevent3)
 * is true. Setting several bits therefore counts *cycles in which any of them
 * fired*, incrementing by at most 1 per cycle -- it is not a sum. Select one
 * event at a time if you want an exact count.
 *
 * This SoC is built with NUM_MHPMCOUNTERS = 1, so there is exactly one
 * selectable event counter. Kernels here are deterministic, so to collect
 * several events you re-run the kernel once per event (perf_measure_event).
 * That was chosen over widening the counter file because the design has only
 * ~9% timing margin at 50 MHz and mhpmcounters are 64 bits wide each.
 * --------------------------------------------------------------------------- */

#ifndef PERF_H
#define PERF_H

#include <stdint.h>

/* Event bit positions, from cv32e40x_cs_registers.sv hpm_events_raw[]. */
typedef enum {
    PERF_EV_CYCLE         = 0,   /* always 1 */
    PERF_EV_INSTRET       = 1,   /* retired instructions */
    PERF_EV_COMPRESSED    = 2,   /* retired compressed instructions */
    PERF_EV_JUMP          = 3,   /* unconditional jumps */
    PERF_EV_BRANCH        = 4,   /* conditional branches */
    PERF_EV_BRANCH_TAKEN  = 5,   /* conditional branches taken */
    PERF_EV_INTR_TAKEN    = 6,   /* interrupts taken (excl. NMI) */
    PERF_EV_DATA_READ     = 7,   /* OBI data-side read transactions */
    PERF_EV_DATA_WRITE    = 8,   /* OBI data-side write transactions */
    PERF_EV_IF_INVALID    = 9,   /* IF had no valid output when ID was ready */
    PERF_EV_ID_INVALID    = 10,  /* ID had no valid output when EX was ready */
    PERF_EV_EX_INVALID    = 11,  /* EX had no valid output when WB was ready */
    PERF_EV_WB_INVALID    = 12,  /* WB had no valid output */
    PERF_EV_LD_STALL      = 13,  /* load-use hazards */
    PERF_EV_JALR_STALL    = 14,  /* jump-register hazards */
    PERF_EV_WB_DATA_STALL = 15,  /* WB stall cycles caused by loads/stores */
    PERF_EV_COUNT         = 16
} perf_event_t;

extern const char *const perf_event_names[PERF_EV_COUNT];

/* ---- raw CSR access ---- */

static inline uint32_t perf_rd_mcycle_lo(void)
{
    uint32_t v; __asm__ volatile("csrr %0, mcycle" : "=r"(v)::"memory"); return v;
}
static inline uint32_t perf_rd_mcycle_hi(void)
{
    uint32_t v; __asm__ volatile("csrr %0, mcycleh" : "=r"(v)::"memory"); return v;
}
static inline uint32_t perf_rd_minstret_lo(void)
{
    uint32_t v; __asm__ volatile("csrr %0, minstret" : "=r"(v)::"memory"); return v;
}
static inline uint32_t perf_rd_minstret_hi(void)
{
    uint32_t v; __asm__ volatile("csrr %0, minstreth" : "=r"(v)::"memory"); return v;
}
static inline uint32_t perf_rd_hpm3_lo(void)
{
    uint32_t v; __asm__ volatile("csrr %0, mhpmcounter3" : "=r"(v)::"memory"); return v;
}
static inline uint32_t perf_rd_hpm3_hi(void)
{
    uint32_t v; __asm__ volatile("csrr %0, mhpmcounter3h" : "=r"(v)::"memory"); return v;
}

static inline void perf_wr_mcountinhibit(uint32_t v)
{
    __asm__ volatile("csrw mcountinhibit, %0" ::"r"(v) : "memory");
}
static inline uint32_t perf_rd_mcountinhibit(void)
{
    uint32_t v; __asm__ volatile("csrr %0, mcountinhibit" : "=r"(v)::"memory"); return v;
}

/* ---- 64-bit reads ----
 * RV32 has no atomic 64-bit CSR read, so re-read the high word and retry if the
 * low word wrapped in between. */
static inline uint64_t perf_cycles(void)
{
    uint32_t hi, lo, hi2;
    do { hi = perf_rd_mcycle_hi(); lo = perf_rd_mcycle_lo(); hi2 = perf_rd_mcycle_hi(); }
    while (hi != hi2);
    return ((uint64_t)hi << 32) | lo;
}

static inline uint64_t perf_instret(void)
{
    uint32_t hi, lo, hi2;
    do { hi = perf_rd_minstret_hi(); lo = perf_rd_minstret_lo(); hi2 = perf_rd_minstret_hi(); }
    while (hi != hi2);
    return ((uint64_t)hi << 32) | lo;
}

static inline uint64_t perf_event_count(void)
{
    uint32_t hi, lo, hi2;
    do { hi = perf_rd_hpm3_hi(); lo = perf_rd_hpm3_lo(); hi2 = perf_rd_hpm3_hi(); }
    while (hi != hi2);
    return ((uint64_t)hi << 32) | lo;
}

/* Select which event mhpmcounter3 counts. One event at a time -- see header. */
static inline void perf_select_event(perf_event_t ev)
{
    uint32_t mask = (uint32_t)1u << (uint32_t)ev;
    __asm__ volatile("csrw mhpmevent3, %0" ::"r"(mask) : "memory");
}

/* Enable all counters. Must be called before any measurement. */
void perf_init(void);

/* Measurement overhead, calibrated at startup by perf_init() through the exact
 * path used for real measurements, and already subtracted by perf_measure*().
 *
 * The event floor is per-event, not a single constant: with PERF_EV_INSTRET
 * selected the counter sees the harness's own CSR reads (~20 instructions),
 * while with PERF_EV_DATA_WRITE it sees almost nothing. A global constant would
 * bias some events and over-correct others. */
extern uint32_t perf_overhead_cycles;
extern uint32_t perf_overhead_instret;
extern uint32_t perf_overhead_event[16];

typedef struct {
    uint64_t cycles;
    uint64_t instret;
    uint64_t event;
    perf_event_t event_id;
} perf_sample_t;

/* Run fn(arg) once, measuring cycles and retired instructions.
 * Overhead is subtracted; the event field is not filled in. */
perf_sample_t perf_measure(void (*fn)(void *), void *arg);

/* Run fn(arg) once with mhpmcounter3 selecting `ev`. */
perf_sample_t perf_measure_event(void (*fn)(void *), void *arg, perf_event_t ev);

#endif /* PERF_H */
