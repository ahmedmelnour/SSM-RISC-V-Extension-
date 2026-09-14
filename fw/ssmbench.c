/* ---------------------------------------------------------------------------
 * ssmbench.c -- FEMBA's SSM kernels at FEMBA's dimensions, on CV32E40X.
 *
 * WHY THIS EXISTS
 * ---------------
 * bench.c's "ssm_scan_q15" is a stand-in, not FEMBA's kernel: int32 state, no
 * d_inner axis, b[t][n]*x[t] in place of a discretised dB', and no
 * discretisation at all. It is fine as a loop-shape probe but it cannot support
 * a claim about FEMBA's SSM. This file ports the real thing:
 * ssm_discretize_q15() and ssm_scan_q15() from
 * BioFoundation/ARES/codegen/runtime/src/kernels/kernel_ssm.c, at FEMBA's
 * dimensions, and measures the fused form against the two-kernel form on the
 * same core with one variable changed.
 *
 * That comparison is the point. Phase 1 measured discretisation at 79% of the
 * SSM's instructions, which caps a scan-only ssm.step at 1.16x end-to-end and
 * a discretisation-inclusive one at 2.82x. Those are GAP9 instruction counts;
 * this is the same question asked in cycles, on RTL we control.
 *
 * WHAT WAS CHANGED IN THE PORT, AND WHY
 * -------------------------------------
 * 1. PULP-isms removed. pi_core_id()/NUM_CORES/pi_cl_team_barrier() drop out
 *    (single core), and SAT_Q15 uses the plain-C form, which is what the
 *    non-__pulp__ arm of the reference already selects.
 *
 * 2. Float index math is a problem, so BOTH forms are measured. The reference
 *    computes the LUT index in float:
 *        z = (dt/65536) * (A/32768);  idx = (int)((z + 10) * (511/10))
 *    GAP9 has an FPU. CV32E40X here is RV32IMC_Zicsr -- it does NOT. Compiled
 *    verbatim, every one of those becomes a libgcc soft-float call, which would
 *    swamp the measurement and tell us nothing about GAP9. So the index is also
 *    implemented in integer arithmetic:
 *        P = dt*A (Q31);  idx = 511 + floor(P*511 / (10 * 2^31))
 *    Both are measured. The integer form is the basis for the fused-vs-two-kernel
 *    comparison; the float form is reported so the soft-float tax is visible
 *    rather than hidden. idx_mismatch below counts where the two disagree.
 *
 * 3. d_inner is tiled. dA and dB' are [seq_len, d_inner, d_state] int16, which
 *    at FEMBA's dimensions is 80*1540*16*2 = 3.9 MB each, 7.9 MB for the pair.
 *    There is no such memory. The kernels are therefore run over a tile of
 *    SSM_TILE channels; per-element cost is what is being measured and this SoC
 *    has no cache, so it does not depend on the tile size -- a claim checked by
 *    running two tile sizes, not assumed.
 *
 *    That 7.9 MB is itself a result: the fused form never materialises dA or
 *    dB' at all, so it removes the entire intermediate, not just the loop
 *    overhead around it.
 *
 * 4. Loop order. Discretisation runs t->m->d; the scan runs m->t->d. The fused
 *    form must pick one, and picks the scan's (the recurrence is the ordering
 *    constraint). Per-element work is independent, so values are unaffected --
 *    asserted by checksum equality against the two-kernel arm, not by argument.
 *
 * See ssm_luts.h for a third, larger discrepancy found while porting: the C
 * runtime's LUTs do not match FEMBA's own Python generator, and 150/512 exp and
 * 255/512 phi1 entries overflow int16_t silently.
 * --------------------------------------------------------------------------- */

#include <stdint.h>
#include "lib/io.h"
#include "lib/perf.h"
#include "ssm_luts.h"

/* ---- FEMBA's dimensions ---------------------------------------------------
 * d_model 385, expand 4 -> d_inner 1540 (in_proj is 385 -> 3080 = 2*d_inner),
 * d_state 16, seq_len 80.  SSM_TILE is the d_inner slice actually run. */
#define SSM_SEQ_LEN   80
#define SSM_D_STATE   16
#define SSM_D_INNER   1540
#ifndef SSM_TILE
#define SSM_TILE      4
#endif

#define N_ELEM  (SSM_SEQ_LEN * SSM_TILE * SSM_D_STATE)

/* ---- working set (.bss: NOLOAD, zeroed by crt0, costs nothing in the image) */
static int16_t dA_q15[SSM_SEQ_LEN * SSM_TILE * SSM_D_STATE];
static int16_t dB_q15[SSM_SEQ_LEN * SSM_TILE * SSM_D_STATE];
static int32_t dt_q16[SSM_SEQ_LEN * SSM_TILE];
static int8_t  x_i8  [SSM_SEQ_LEN * SSM_TILE];
static int32_t y_acc [SSM_SEQ_LEN * SSM_TILE];
static int32_t y_ref [SSM_SEQ_LEN * SSM_TILE];
static int16_t A_q15 [SSM_D_STATE * SSM_TILE];
static int16_t B_q15 [SSM_SEQ_LEN * SSM_D_STATE];
static int16_t C_q15 [SSM_SEQ_LEN * SSM_D_STATE];
static int16_t D_q15 [SSM_TILE];
static int16_t h_q15 [SSM_TILE * SSM_D_STATE];
static int16_t h_ref [SSM_TILE * SSM_D_STATE];

static int16_t lam_q15_arr[SSM_SEQ_LEN * SSM_TILE];   /* data-dependent lambda */
static int32_t Bs_prev[SSM_TILE * SSM_D_STATE];       /* B_{t-1}*s_x, per lane   */
static int8_t  x_prev[SSM_TILE];                      /* x_{t-1}, per channel    */

static int16_t s_x_q15 = 12345;

static int16_t om_nh[SSM_TILE], om_dt[SSM_TILE];
static int16_t om_Bt[SSM_D_STATE], om_Ct[SSM_D_STATE];

volatile int32_t checksum;

/* Q15 saturation -- the plain-C arm of the reference's SAT_Q15.
 * int32 only: the reference notes that the int64 dB' chain must NOT use it. */
static inline int32_t sat_q15(int32_t v)
{
    return v > 32767 ? 32767 : (v < -32768 ? -32768 : v);
}

/* ---- LUT index, two ways -------------------------------------------------- */

/* Verbatim from the reference: float, hence soft-float on this core. */
static inline int lut_idx_flt(int32_t dt_val, int16_t A_val)
{
    const float lut_min = -10.0f;
    const float lut_max = 0.0f;
    const int   lut_size = SSM_EXP_LUT_SIZE;
    const float lut_step = (lut_max - lut_min) / (lut_size - 1);
    const float inv_step = 1.0f / lut_step;

    float dt_f = (float)dt_val / 65536.0f;
    float A_f  = (float)A_val / 32768.0f;
    float z    = dt_f * A_f;
    float idx_f = (z - lut_min) * inv_step;
    int idx = (int)idx_f;
    if (idx < 0) idx = 0;
    if (idx >= lut_size) idx = lut_size - 1;
    return idx;
}

/* Integer equivalent. z = dt*A / 2^31 (Q16 * Q15 = Q31), so
 *     idx = (z + 10) * 511/10 = 511 + P*511 / (10 * 2^31),  P = dt*A.
 * The float path truncates a non-negative idx_f, i.e. floors it, so this must
 * floor too -- C division truncates toward zero, which for the negative P*511
 * would round the wrong way and land one index high. */
static inline int lut_idx_int(int32_t dt_val, int16_t A_val)
{
    const int64_t DEN = 10LL << 31;                 /* 10 * 2^31 */
    int64_t num = (int64_t)dt_val * (int64_t)A_val * 511;
    int64_t q = (num >= 0) ? (num / DEN) : -(((-num) + DEN - 1) / DEN);
    int64_t idx = 511 + q;
    if (idx < 0) idx = 0;
    if (idx >= SSM_EXP_LUT_SIZE) idx = SSM_EXP_LUT_SIZE - 1;
    return (int)idx;
}

/* ---- deterministic filler ------------------------------------------------- */
static uint32_t rng_state = 0x12345678u;
static uint32_t rnd(void) { rng_state = rng_state * 1664525u + 1013904223u; return rng_state; }

static void fill_inputs(void)
{
    /* dt comes from softplus, so it is positive; A is negative (the reference
     * says so explicitly). Ranges chosen so z = dt*A lands inside the LUT's
     * [-10, 0] domain for most elements rather than pinning at an endpoint,
     * which would flatten the clamp paths and bias the instruction mix. */
    for (int i = 0; i < SSM_SEQ_LEN * SSM_TILE; i++) {
        dt_q16[i] = (int32_t)(rnd() % 131072u) + 1024;        /* ~0.016 .. 2.0  */
        x_i8[i]   = (int8_t)(rnd() % 256u);
    }
    for (int i = 0; i < SSM_D_STATE * SSM_TILE; i++)
        A_q15[i] = (int16_t)(-1 - (int32_t)(rnd() % 32767u)); /* -1 .. -32768   */
    for (int i = 0; i < SSM_SEQ_LEN * SSM_D_STATE; i++) {
        B_q15[i] = (int16_t)((rnd() % 16384u) - 8192);
        C_q15[i] = (int16_t)((rnd() % 16384u) - 8192);
    }
    for (int i = 0; i < SSM_TILE; i++)
        D_q15[i] = (int16_t)((rnd() % 16384u) - 8192);
    for (int i = 0; i < SSM_SEQ_LEN * SSM_TILE; i++)
        lam_q15_arr[i] = (int16_t)(rnd() % 32768u);   /* lambda in [0,1) Q15 */
    for (int i = 0; i < SSM_TILE; i++) {
        om_nh[i] = (int16_t)((rnd() % 16384u) - 8192);
        om_dt[i] = (int16_t)(rnd() % 16384u);
    }
    for (int i = 0; i < SSM_D_STATE; i++) {
        om_Bt[i] = (int16_t)((rnd() % 16384u) - 8192);
        om_Ct[i] = (int16_t)((rnd() % 16384u) - 8192);
    }
}

/* ---- the two kernels, ported ---------------------------------------------
 * USE_FLT selects the index path; everything else is identical between arms. */

#define DISCRETIZE_BODY(IDXFN)                                                 \
    for (int t = 0; t < SSM_SEQ_LEN; t++) {                                    \
        int32_t Bs_row[SSM_D_STATE];                                           \
        for (int d = 0; d < SSM_D_STATE; d++)                                  \
            Bs_row[d] = (int32_t)B_q15[t * SSM_D_STATE + d] * (int32_t)s_x_q15;\
        for (int m = 0; m < SSM_TILE; m++) {                                   \
            int32_t dt_val = dt_q16[t * SSM_TILE + m];                         \
            for (int d = 0; d < SSM_D_STATE; d++) {                            \
                int16_t A_val = A_q15[d * SSM_TILE + m];                       \
                int idx = IDXFN(dt_val, A_val);                                \
                int32_t oi = (t * SSM_TILE + m) * SSM_D_STATE + d;             \
                dA_q15[oi] = ssm_exp_lut_q15[idx];                             \
                int16_t phi1_val = ssm_phi1_lut_q15[idx];                      \
                int64_t dB_temp =                                              \
                    (((int64_t)dt_val * (int64_t)Bs_row[d]                     \
                      * (int64_t)phi1_val) >> 31);                             \
                if (dB_temp > 32767) dB_temp = 32767;                          \
                if (dB_temp < -32768) dB_temp = -32768;                        \
                dB_q15[oi] = (int16_t)dB_temp;                                 \
            }                                                                  \
        }                                                                      \
    }

static void k_discretize_int(void *arg) { (void)arg; DISCRETIZE_BODY(lut_idx_int) }
static void k_discretize_flt(void *arg) { (void)arg; DISCRETIZE_BODY(lut_idx_flt) }

/* The scan, verbatim in structure. Reads dA/dB' back out of memory. */
static void k_scan(void *arg)
{
    (void)arg;
    for (int m = 0; m < SSM_TILE; m++) {
        for (int d = 0; d < SSM_D_STATE; d++) h_q15[m * SSM_D_STATE + d] = 0;

        for (int t = 0; t < SSM_SEQ_LEN; t++) {
            int8_t x_val = x_i8[t * SSM_TILE + m];
            int32_t y_sum = 0;
            for (int d = 0; d < SSM_D_STATE; d++) {
                int idx = (t * SSM_TILE + m) * SSM_D_STATE + d;
                int16_t dA = dA_q15[idx];
                int16_t dB_prime = dB_q15[idx];
                int16_t h_prev = h_q15[m * SSM_D_STATE + d];
                int16_t C_val = C_q15[t * SSM_D_STATE + d];

                int32_t h_decay = ((int32_t)dA * (int32_t)h_prev) >> 15;
                int32_t h_input = ((int32_t)dB_prime * (int32_t)x_val);
                int32_t h_new = sat_q15(h_decay + h_input);
                h_q15[m * SSM_D_STATE + d] = (int16_t)h_new;

                y_sum += ((int32_t)C_val * h_new) >> 15;
            }
            y_sum += ((int32_t)D_q15[m] * (int32_t)x_val);
            y_acc[t * SSM_TILE + m] = y_sum;
        }
    }
}

/* ---- the fused form -------------------------------------------------------
 * One pass. dA and dB' are produced into registers and consumed immediately;
 * neither array is ever written or read back. Arithmetic is identical to the
 * two-kernel arm element for element -- dB' is clipped to Q15 exactly as the
 * two-kernel arm clips it before its int16 store, so the fused result is
 * bit-exact rather than merely close. (Keeping dB' at full width instead is a
 * different, numerical proposal; phase 0 measured it and it is deliberately not
 * what is timed here, because it would change the output and stop being a
 * one-variable comparison.)
 *
 * Bs_row cannot be hoisted to the t loop here: the fused order is m->t->d, so
 * B*s_x is recomputed per (m,t). That is a real cost of fusing in this order
 * and it is left in rather than optimised away. */
#define FUSED_BODY(IDXFN)                                                      \
    for (int m = 0; m < SSM_TILE; m++) {                                       \
        for (int d = 0; d < SSM_D_STATE; d++) h_q15[m * SSM_D_STATE + d] = 0;  \
        for (int t = 0; t < SSM_SEQ_LEN; t++) {                                \
            int8_t  x_val  = x_i8[t * SSM_TILE + m];                           \
            int32_t dt_val = dt_q16[t * SSM_TILE + m];                         \
            int32_t y_sum  = 0;                                                \
            for (int d = 0; d < SSM_D_STATE; d++) {                            \
                int16_t A_val = A_q15[d * SSM_TILE + m];                       \
                int idx = IDXFN(dt_val, A_val);                                \
                int16_t dA = ssm_exp_lut_q15[idx];                             \
                int16_t phi1_val = ssm_phi1_lut_q15[idx];                      \
                int32_t Bs = (int32_t)B_q15[t * SSM_D_STATE + d]               \
                           * (int32_t)s_x_q15;                                 \
                int64_t dB_temp = (((int64_t)dt_val * (int64_t)Bs              \
                                    * (int64_t)phi1_val) >> 31);               \
                if (dB_temp > 32767) dB_temp = 32767;                          \
                if (dB_temp < -32768) dB_temp = -32768;                        \
                int16_t dB_prime = (int16_t)dB_temp;                           \
                                                                               \
                int16_t h_prev = h_q15[m * SSM_D_STATE + d];                   \
                int16_t C_val  = C_q15[t * SSM_D_STATE + d];                   \
                int32_t h_decay = ((int32_t)dA * (int32_t)h_prev) >> 15;       \
                int32_t h_input = ((int32_t)dB_prime * (int32_t)x_val);        \
                int32_t h_new = sat_q15(h_decay + h_input);                    \
                h_q15[m * SSM_D_STATE + d] = (int16_t)h_new;                   \
                y_sum += ((int32_t)C_val * h_new) >> 15;                       \
            }                                                                  \
            y_sum += ((int32_t)D_q15[m] * (int32_t)x_val);                     \
            y_acc[t * SSM_TILE + m] = y_sum;                                   \
        }                                                                      \
    }

static void k_fused_int(void *arg) { (void)arg; FUSED_BODY(lut_idx_int) }
static void k_fused_flt(void *arg) { (void)arg; FUSED_BODY(lut_idx_flt) }

/* Two-kernel arms as one measurable unit: discretise, then scan. */
static void k_twokernel_int(void *arg) { k_discretize_int(arg); k_scan(arg); }
static void k_twokernel_flt(void *arg) { k_discretize_flt(arg); k_scan(arg); }



/* ---- DISCRETISATION LADDER -----------------------------------------------
 * Three rules over the same recurrence, same LUT, same data, same lane shape.
 * Only the input term changes, so the cycle difference is the cost of the rule.
 *
 *   FEMBA / exact ZOH   dB' = (dt * B*s_x * phi1(dtA)) >> 31   <- k_fused_int
 *   exponential-Euler   dB' = (dt * B*s_x) >> 16               <- k_fused_expeuler
 *                       what Mamba-1/-2 RELEASED CODE actually implements, per
 *                       Mamba-3 (arXiv 2603.15569) Sec 3.1 Table 1 -- the papers
 *                       claim ZOH. So FEMBA's phi1 is their own accuracy choice,
 *                       not something Mamba's semantics require. This arm prices it.
 *   exp-trapezoidal     h = a*h + beta*B_{t-1}x_{t-1} + gamma*B_t x_t
 *                       Mamba-3 Prop. 1: a = e^{dtA}, beta = (1-lam)*dt*e^{dtA},
 *                       gamma = lam*dt, lam data-dependent in [0,1].
 *                       lam = 1 collapses to exponential-Euler.
 *
 * The point of the trapezoidal arm: alpha IS dA, so it reads the SAME LUT entry
 * the other two already read. The extra cost is one retained input per lane, two
 * coefficient multiplies and one extra multiply-add -- not a second datapath.
 *
 * These arms compute DIFFERENT VALUES from each other by construction; they are
 * different discretisation rules. There is no bit-exactness check between them,
 * only within each (fused vs two-kernel).
 * ------------------------------------------------------------------------- */

/* exponential-Euler: FEMBA's kernel with the phi1 factor removed. */
static void k_fused_expeuler(void *arg)
{
    (void)arg;
    for (int m = 0; m < SSM_TILE; m++) {
        for (int d = 0; d < SSM_D_STATE; d++) h_q15[m * SSM_D_STATE + d] = 0;
        for (int t = 0; t < SSM_SEQ_LEN; t++) {
            int8_t  x_val  = x_i8[t * SSM_TILE + m];
            int32_t dt_val = dt_q16[t * SSM_TILE + m];
            int32_t y_sum  = 0;
            for (int d = 0; d < SSM_D_STATE; d++) {
                int idx = lut_idx_int(dt_val, A_q15[d * SSM_TILE + m]);
                int16_t dA = ssm_exp_lut_q15[idx];
                int32_t Bs = (int32_t)B_q15[t * SSM_D_STATE + d] * (int32_t)s_x_q15;

                /* one multiply and one LUT read fewer than ZOH */
                int64_t dB_temp = (((int64_t)dt_val * (int64_t)Bs) >> 16);
                if (dB_temp > 32767) dB_temp = 32767;
                if (dB_temp < -32768) dB_temp = -32768;
                int16_t dB_prime = (int16_t)dB_temp;

                int16_t h_prev = h_q15[m * SSM_D_STATE + d];
                int32_t h_decay = ((int32_t)dA * (int32_t)h_prev) >> 15;
                int32_t h_new = sat_q15(h_decay
                                        + (int32_t)dB_prime * (int32_t)x_val);
                h_q15[m * SSM_D_STATE + d] = (int16_t)h_new;
                y_sum += ((int32_t)C_q15[t * SSM_D_STATE + d] * h_new) >> 15;
            }
            y_sum += ((int32_t)D_q15[m] * (int32_t)x_val);
            y_acc[t * SSM_TILE + m] = y_sum;
        }
    }
}

/* exponential-trapezoidal (Mamba-3 Prop. 1), second order. */
static void k_fused_trapz(void *arg)
{
    (void)arg;
    for (int m = 0; m < SSM_TILE; m++) {
        for (int d = 0; d < SSM_D_STATE; d++) {
            h_q15[m * SSM_D_STATE + d] = 0;
            Bs_prev[m * SSM_D_STATE + d] = 0;
        }
        x_prev[m] = 0;

        for (int t = 0; t < SSM_SEQ_LEN; t++) {
            int8_t  x_val  = x_i8[t * SSM_TILE + m];
            int32_t dt_val = dt_q16[t * SSM_TILE + m];
            int16_t lam    = lam_q15_arr[t * SSM_TILE + m];
            int32_t y_sum  = 0;

            /* gamma = lam*dt ; beta = (1-lam)*dt*alpha -- alpha folded in below,
             * since it is per-lane. Both are Q16. */
            int32_t gamma = (int32_t)(((int64_t)lam * (int64_t)dt_val) >> 15);
            int32_t omlam = (int32_t)(((int64_t)(32767 - lam) * (int64_t)dt_val) >> 15);

            for (int d = 0; d < SSM_D_STATE; d++) {
                int idx = lut_idx_int(dt_val, A_q15[d * SSM_TILE + m]);
                int16_t dA = ssm_exp_lut_q15[idx];          /* alpha -- same entry */
                int32_t Bs = (int32_t)B_q15[t * SSM_D_STATE + d] * (int32_t)s_x_q15;

                /* new-input coefficient: gamma * B_t */
                int64_t g_t = (((int64_t)gamma * (int64_t)Bs) >> 16);
                if (g_t > 32767) g_t = 32767;
                if (g_t < -32768) g_t = -32768;

                /* old-input coefficient: (1-lam)*dt*alpha * B_{t-1} */
                int64_t beta = ((int64_t)omlam * (int64_t)dA) >> 15;
                int64_t b_t  = ((beta * (int64_t)Bs_prev[m * SSM_D_STATE + d]) >> 16);
                if (b_t > 32767) b_t = 32767;
                if (b_t < -32768) b_t = -32768;

                int16_t h_prev = h_q15[m * SSM_D_STATE + d];
                int32_t h_decay = ((int32_t)dA * (int32_t)h_prev) >> 15;
                int32_t h_new = sat_q15(h_decay
                                        + (int32_t)g_t * (int32_t)x_val
                                        + (int32_t)b_t * (int32_t)x_prev[m]);
                h_q15[m * SSM_D_STATE + d] = (int16_t)h_new;
                Bs_prev[m * SSM_D_STATE + d] = Bs;          /* the retained input */

                y_sum += ((int32_t)C_q15[t * SSM_D_STATE + d] * h_new) >> 15;
            }
            x_prev[m] = x_val;
            y_sum += ((int32_t)D_q15[m] * (int32_t)x_val);
            y_acc[t * SSM_TILE + m] = y_sum;
        }
    }
}

/* ---- the OWN-MODEL arm ---------------------------------------------------
 * Extracted verbatim from ssm_model.c's ssm_infer() inner nest -- the SSM this
 * project's own MIT-BIH model actually runs. It is a DIFFERENT ALGORITHM from
 * FEMBA's, not a different coding of the same one:
 *
 *   FEMBA        dA  = exp_lut[idx(dt*A)]          (512-entry LUT, float index)
 *                dB' = (dt * B * s_x * phi1[idx]) >> 31      (int64 chain)
 *                written to [seq_len, d_inner, d_state] arrays, read back
 *
 *   own model    a   = 1 - dt*A, clamped to [0, 0.999]       (first-order Euler)
 *                bb  = (dt*x) * B                            (no phi1 at all)
 *                never materialised -- already inside the scan's inner loop
 *
 * So it has no exp LUT, no phi1 LUT, no int64, and no intermediate arrays. It is
 * measured here at the SAME dimensions as the FEMBA arms so the per-element
 * costs are directly comparable. Scale factors are ssm_model.c's own.
 * ------------------------------------------------------------------------- */
static int16_t om_state[SSM_TILE * SSM_D_STATE];

static inline int32_t om_rsr(int32_t v, int s)
{
    if (s == 0) return v;
    if (s < 0)  return (int32_t)((uint32_t)v << (-s));
    return (v + ((int32_t)1 << (s - 1))) >> s;
}

/* ssm_model.c's fixed-point exponents, verbatim from model_data.h */
#define OM_FA_NORM 13
#define OM_FA_DT   12
#define OM_FA_DTX  12
#define OM_FA_BC   13
#define OM_FA_H     9
#define OM_FA_A    15

static void k_ownmodel(void *arg)
{
    (void)arg;
    const int SP = 3;                                   /* ilog2(16) - 1 */
    const int s_dtx = OM_FA_DT + OM_FA_NORM - OM_FA_DTX;
    const int s_b   = OM_FA_DTX + OM_FA_BC - OM_FA_H;
    const int32_t ONE  = (int32_t)1 << OM_FA_A;
    const int32_t AMAX = (int32_t)(0.999 * (1 << OM_FA_A));

    for (int i = 0; i < SSM_TILE * SSM_D_STATE; i++) om_state[i] = 0;

    for (int t = 0; t < SSM_SEQ_LEN; t++) {
        for (int dd = 0; dd < SSM_TILE; dd++) {
            int32_t dtx = om_rsr((int32_t)om_dt[dd] * (int32_t)om_nh[dd], s_dtx);
            if (dtx > 32767) dtx = 32767;
            if (dtx < -32768) dtx = -32768;

            int32_t acc = 0;
            for (int nn = 0; nn < SSM_D_STATE; nn++) {
                int32_t dtA = om_rsr((int32_t)om_dt[dd]
                                     * (int32_t)A_q15[nn * SSM_TILE + dd],
                                     OM_FA_DT + 15 - OM_FA_A);
                int32_t a = ONE - dtA;
                if (a < 0)    a = 0;
                if (a > AMAX) a = AMAX;

                int32_t bb = om_rsr(dtx * (int32_t)om_Bt[nn], s_b);
                int32_t hv = om_rsr(a * (int32_t)om_state[dd * SSM_D_STATE + nn],
                                    OM_FA_A) + bb;
                om_state[dd * SSM_D_STATE + nn] = sat_q15(hv);

                acc += om_rsr((int32_t)om_state[dd * SSM_D_STATE + nn]
                              * (int32_t)om_Ct[nn], SP);
            }
            y_acc[t * SSM_TILE + dd] = acc;
        }
    }
}

/* ---- harness -------------------------------------------------------------- */

typedef struct { const char *name; void (*fn)(void *); } kernel_t;

/* SSM_QUICK cuts simulation time WITHOUT changing how anything is compiled: the
 * kernel table below is unconditional, so codegen -- and therefore every cycle
 * count -- is identical in both modes. It only narrows the event sweep to
 * instructions and skips the two setup checks that are themselves expensive
 * (the soft-float index cross-check and the float bit-exactness pass), whose
 * answers do not vary with tile size.
 *
 * An earlier version also dropped kernels from this table. That let GCC inline
 * k_discretize_int and k_scan into k_twokernel_int and delete the standalone
 * copies, which moved the baseline by 0.4% and made the quick run disagree with
 * the full one. Do not reintroduce that. */
static const kernel_t kernels[] = {
    { "discretize_int", k_discretize_int },
    { "scan",           k_scan           },
    { "twokernel_int",  k_twokernel_int  },
    { "fused_int",      k_fused_int      },
    { "twokernel_flt",  k_twokernel_flt  },
    { "fused_flt",      k_fused_flt      },
    { "fused_expeuler", k_fused_expeuler },
    { "fused_trapz",    k_fused_trapz    },
    { "ownmodel",       k_ownmodel       },
};
#define NUM_KERNELS ((int)(sizeof(kernels) / sizeof(kernels[0])))

static const perf_event_t sweep[] = {
    PERF_EV_INSTRET,
#ifndef SSM_QUICK
    PERF_EV_BRANCH_TAKEN,
    PERF_EV_DATA_READ,
    PERF_EV_DATA_WRITE,
    PERF_EV_LD_STALL,
#endif
};
#define NUM_SWEEP ((int)(sizeof(sweep) / sizeof(sweep[0])))

static void emit_cfg(const char *k, uint64_t v)
{
    uart_puts("#CFG,"); uart_puts(k); uart_putc(','); uart_put_u64(v); uart_puts("\n");
}
static void emit_cfg_s(const char *k, const char *v)
{
    uart_puts("#CFG,"); uart_puts(k); uart_putc(','); uart_puts(v); uart_puts("\n");
}

/* Fold the whole output into one value so -O2 cannot delete the kernel. */
static int32_t digest(void)
{
    int32_t s = 0;
    for (int i = 0; i < SSM_SEQ_LEN * SSM_TILE; i++) s = s * 31 + y_acc[i];
    for (int i = 0; i < SSM_TILE * SSM_D_STATE; i++) s = s * 31 + h_q15[i];
    return s;
}

int main(void)
{
    perf_init();
    fill_inputs();

    uart_puts("\n#PERF,1\n");
    emit_cfg_s("program", "ssmbench");
    emit_cfg_s("lut", SSM_LUT_NAME);
#ifdef SSM_QUICK
    emit_cfg_s("mode", "quick");
#else
    emit_cfg_s("mode", "full");
#endif
    emit_cfg("clk_hz", 50000000u);
    emit_cfg("seq_len", SSM_SEQ_LEN);
    emit_cfg("d_state", SSM_D_STATE);
    emit_cfg("d_inner_full", SSM_D_INNER);
    emit_cfg("tile", SSM_TILE);
    emit_cfg("elements", N_ELEM);
    emit_cfg("tiles_for_full_d_inner", SSM_D_INNER / SSM_TILE);
    emit_cfg("overhead_cycles", perf_overhead_cycles);
    emit_cfg("overhead_instret", perf_overhead_instret);
    emit_cfg("mcountinhibit", perf_rd_mcountinhibit());
    emit_cfg("num_kernels", NUM_KERNELS);
    emit_cfg("num_events", NUM_SWEEP);

    /* How faithful is the integer index to the float one it replaces?
     * Measured over exactly the operands the kernels use, outside any timed
     * region. A non-zero count here is a caveat on every _int number below. */
    {
        uint32_t mism = 0;
        for (int t = 0; t < SSM_SEQ_LEN; t++)
            for (int m = 0; m < SSM_TILE; m++)
                for (int d = 0; d < SSM_D_STATE; d++)
                    if (lut_idx_int(dt_q16[t * SSM_TILE + m], A_q15[d * SSM_TILE + m])
                        != lut_idx_flt(dt_q16[t * SSM_TILE + m], A_q15[d * SSM_TILE + m]))
                        mism++;
        emit_cfg("idx_mismatch_int_vs_flt", mism);
        emit_cfg("idx_compared", N_ELEM);
    }

    /* Is the fused form bit-exact against the two-kernel form? Establish the
     * reference, then check each fused arm against it. */
    k_twokernel_int(0);
    for (int i = 0; i < SSM_SEQ_LEN * SSM_TILE; i++) y_ref[i] = y_acc[i];
    for (int i = 0; i < SSM_TILE * SSM_D_STATE; i++) h_ref[i] = h_q15[i];
    int32_t ref_digest = digest();

    k_fused_int(0);
    int fused_exact = (digest() == ref_digest);
    for (int i = 0; i < SSM_SEQ_LEN * SSM_TILE; i++)
        if (y_acc[i] != y_ref[i]) { fused_exact = 0; break; }
    emit_cfg("fused_int_bitexact_vs_twokernel", (uint64_t)fused_exact);

#ifndef SSM_QUICK
    k_twokernel_flt(0);
    int32_t flt_digest = digest();
    k_fused_flt(0);
    emit_cfg("fused_flt_bitexact_vs_twokernel_flt", (uint64_t)(digest() == flt_digest));
    emit_cfg("int_path_matches_flt_path", (uint64_t)(ref_digest == flt_digest));
#endif

    uart_puts("#COLS,kernel,reps,cycles,instret,ev_id,ev_name,ev_count,checksum\n");

    for (int ki = 0; ki < NUM_KERNELS; ki++) {
        for (int ei = 0; ei < NUM_SWEEP; ei++) {
            perf_sample_t s = perf_measure_event(kernels[ki].fn, 0, sweep[ei]);
            checksum += digest();

            uart_puts("#DATA,");
            uart_puts(kernels[ki].name);              uart_putc(',');
            uart_put_u32(1);                          uart_putc(',');
            uart_put_u64(s.cycles);                   uart_putc(',');
            uart_put_u64(s.instret);                  uart_putc(',');
            uart_put_u32((uint32_t)s.event_id);       uart_putc(',');
            uart_puts(perf_event_names[s.event_id]);  uart_putc(',');
            uart_put_u64(s.event);                    uart_putc(',');
            uart_put_hex32((uint32_t)checksum);
            uart_puts("\n");
        }
    }

    uart_puts("#END\n");
    return 0;
}
