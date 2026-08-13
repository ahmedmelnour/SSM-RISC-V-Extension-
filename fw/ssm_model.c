/* ---------------------------------------------------------------------------
 * ssm_model.c -- integer inference for the selective SSM, for CV32E40X.
 *
 * This is a transcription of py/quantize.py and must match it **bit for bit**.
 * It is not "the same algorithm in C"; every shift, every saturation and every
 * division rounds the way the Python reference rounds, because the week-2 gate
 * is exactly the claim that these two produce identical integers.
 *
 * Three places where the obvious C is silently wrong:
 *
 * 1. **numpy `//` floors, C `/` truncates.** They differ for negative operands,
 *    and both divisions here (the LayerNorm mean and the mean-pool) run on sums
 *    that go negative. `fdiv()` below floors explicitly. Getting this wrong
 *    produces an off-by-one that only appears on negative inputs -- which is to
 *    say, on about half of them, but never on a test vector of pure-N beats.
 * 2. **`cen >> SV` is a plain arithmetic shift, not a rounded one.** The
 *    reference uses `>>` there and `rshift_round` everywhere else. Rounding it
 *    "for consistency" changes the variance and breaks the match.
 * 3. **A shift can be negative**, meaning a left shift -- the mean-pool rescale
 *    is one (FA_RES < FA_POOL). A right-shift-only helper gets this wrong.
 *
 * Memory: the residual stream [T][D] as int16 is the only large buffer (18 KB).
 * Everything else is per-timestep, because the block can be evaluated one
 * timestep at a time: at step t it reads h_res[t] and writes h_res[t], and
 * never touches that index again, so updating in place is safe and avoids a
 * second [T][D] array.
 * --------------------------------------------------------------------------- */

#include "ssm_model.h"
#include "model_data.h"

/* ------------------------------------------------------------------ helpers */

/* Arithmetic right shift with round-to-nearest, ties away from zero.
 * Negative s means a left shift. Mirrors fixedpoint.rshift_round. */
static inline int32_t rsr(int32_t v, int s)
{
    if (s == 0) return v;
    if (s < 0)  return (int32_t)((uint32_t)v << (-s));
    return (v + ((int32_t)1 << (s - 1))) >> s;
}

/* Floor division, matching numpy's `//`. C's `/` truncates toward zero. */
static inline int32_t fdiv(int32_t a, int32_t b)
{
    int32_t q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) q--;
    return q;
}

static inline int16_t sat16(int32_t v)
{
    if (v > 32767)  return 32767;
    if (v < -32768) return -32768;
    return (int16_t)v;
}

/* floor(sqrt(n)) by restoring square root -- no division, no float.
 * Same algorithm as quantize.isqrt_int. */
static uint32_t isqrt32(uint32_t n)
{
    uint32_t x = 0, bit = 1u << 30, rem = n;
    while (bit > rem) bit >>= 2;
    while (bit) {
        uint32_t t = x + bit;
        if (rem >= t) { rem -= t; x = (x >> 1) + bit; }
        else          { x >>= 1; }
        bit >>= 2;
    }
    return x;
}

static inline int ilog2_u32(uint32_t v)      /* floor(log2(v)), v >= 1 */
{
    int r = 0;
    while (v >>= 1) r++;
    return r;
}

/* y = affine(x) with per-output-channel weight shifts, fa_in -> fa_out. */
static void linear_i16(const int16_t *x, int n_in, int n_out,
                       const int8_t *w, const int32_t *bias, const int8_t *wsh,
                       int fa_in, int fa_out, int16_t *y)
{
    for (int o = 0; o < n_out; o++) {
        const int8_t *wr = w + (int)o * n_in;
        int32_t acc = bias ? bias[o] : 0;
        for (int i = 0; i < n_in; i++)
            acc += (int32_t)x[i] * (int32_t)wr[i];
        y[o] = sat16(rsr(acc, (int)wsh[o] + fa_in - fa_out));
    }
}

/* LayerNorm over D, fa_in -> fa_out. Mirrors quantize.layernorm_int. */
static void layernorm(const int16_t *x, int D,
                      const int8_t *g, int g_sh, const int32_t *b,
                      int fa_in, int fa_out, int16_t *y)
{
    int32_t sum = 0;
    for (int i = 0; i < D; i++) sum += x[i];
    int32_t mean = fdiv(sum, D);

    /* SV derived from the int32 bound, not hardcoded: |cen| <= 2**16 gives
     * sum_D (cen>>SV)^2 <= 2**(32-2SV) * D <= 2**31-1, so SV >= (1+log2 D)/2. */
    const int SV = (1 + ilog2_u32((uint32_t)D) + 1) / 2;

    int32_t vacc = 0;
    for (int i = 0; i < D; i++) {
        int32_t cs = ((int32_t)x[i] - mean) >> SV;   /* plain shift, not rounded */
        vacc += cs * cs;
    }
    int32_t var = fdiv(vacc, D);
    if (var < 1) var = 1;

    /* Scale var up to the top of int32 before the sqrt: var is typically ~2**13
     * while int32 holds 2**31, and a plain isqrt of that carries only ~1%
     * resolution, which lands straight on the normalised output. */
    int bl = ilog2_u32((uint32_t)var) + 1;
    int E  = (30 - bl) / 2;
    int Emax = 30 - (2 * fa_in - SV);
    if (E > Emax) E = Emax;
    if (E < 0)    E = 0;

    uint32_t sq = isqrt32((uint32_t)var << (2 * E));
    if (sq < 1) sq = 1;

    int32_t r = (int32_t)(((uint32_t)1 << (2 * fa_in - SV + E)) / sq);
    int32_t rmax = (int32_t)1 << (fa_in + 3);
    if (r > rmax) r = rmax;
    if (r < 1)    r = 1;

    for (int i = 0; i < D; i++) {
        int32_t cen = (int32_t)x[i] - mean;
        int32_t v = rsr(cen * r, 2 * fa_in - fa_out);
        v = rsr(v * (int32_t)g[i], g_sh) + b[i];
        y[i] = sat16(v);
    }
}

/* softplus via the exported LUT with linear interpolation, at FA_DTP.
 * Mirrors fixedpoint.softplus_int. */
static int32_t softplus_dtp(int32_t z)
{
    const int32_t lo = SP_LUT_LO, hi = SP_LUT_HI;
    if (z <= lo) return 0;
    if (z >= hi) return z;
    int32_t span = hi - lo;
    int32_t idx_f = (z - lo) * (SP_LUT_N - 1);
    int32_t idx = idx_f / span;                 /* both positive here */
    if (idx > SP_LUT_N - 2) idx = SP_LUT_N - 2;
    if (idx < 0) idx = 0;
    int32_t frac = idx_f - idx * span;
    int32_t t0 = sp_lut[idx], t1 = sp_lut[idx + 1];
    return t0 + fdiv((t1 - t0) * frac, span);
}

/* ------------------------------------------------------------------ the model */

static int16_t h_res[M_T][M_D];       /* residual stream, the only big buffer */
static int16_t state[M_D][M_N];       /* SSM state */

/* Per-layer parameter tables, so the block loop can be written once. */
static const int8_t  *const NORM_W[M_L]   = { norm0_w, norm1_w };
static const int32_t *const NORM_B[M_L]   = { norm0_b, norm1_b };
static const int     NORM_SH[M_L]         = { NORM0_W_SH, NORM1_W_SH };
static const int16_t *const A_Q[M_L]      = { blk0_A, blk1_A };
static const int     A_SH[M_L]            = { BLK0_A_SH, BLK1_A_SH };
static const int8_t  *const DSKIP[M_L]    = { blk0_dskip, blk1_dskip };
static const int     DSKIP_SH[M_L]        = { BLK0_DSKIP_SH, BLK1_DSKIP_SH };
static const int8_t  *const DT_W[M_L]     = { blk0_dt_proj_w, blk1_dt_proj_w };
static const int8_t  *const DT_SH[M_L]    = { blk0_dt_proj_w_sh, blk1_dt_proj_w_sh };
static const int32_t *const DT_B[M_L]     = { blk0_dt_proj_b, blk1_dt_proj_b };
static const int8_t  *const B_W[M_L]      = { blk0_B_proj_w, blk1_B_proj_w };
static const int8_t  *const B_SH[M_L]     = { blk0_B_proj_w_sh, blk1_B_proj_w_sh };
static const int32_t *const B_B[M_L]      = { blk0_B_proj_b, blk1_B_proj_b };
static const int8_t  *const C_W[M_L]      = { blk0_C_proj_w, blk1_C_proj_w };
static const int8_t  *const C_SH[M_L]     = { blk0_C_proj_w_sh, blk1_C_proj_w_sh };
static const int32_t *const C_B[M_L]      = { blk0_C_proj_b, blk1_C_proj_b };
static const int8_t  *const O_W[M_L]      = { blk0_out_proj_w, blk1_out_proj_w };
static const int8_t  *const O_SH[M_L]     = { blk0_out_proj_w_sh, blk1_out_proj_w_sh };
static const int32_t *const O_B[M_L]      = { blk0_out_proj_b, blk1_out_proj_b };

void ssm_infer(const int16_t *x, int32_t *logits)
{
    /* in_proj: one scalar input per timestep -> D channels. */
    for (int t = 0; t < M_T; t++) {
        int16_t xt = x[t];
        for (int o = 0; o < M_D; o++) {
            int32_t acc = in_proj_b[o] + (int32_t)xt * (int32_t)in_proj_w[o];
            h_res[t][o] = sat16(rsr(acc, (int)in_proj_w_sh[o] + FA_INPUT - FA_RES));
        }
    }

    /* SP: N * (2**15 * 2**15 >> SP) <= 2**31  =>  SP >= log2(N) - 1. */
    int SP = ilog2_u32((uint32_t)M_N) - 1;
    if (SP < 0) SP = 0;

    const int s_dtx = FA_DT + FA_NORM - FA_DTX;
    const int s_b   = FA_DTX + FA_BC - FA_H;
    const int32_t ONE  = (int32_t)1 << FA_A;
    const int32_t AMAX = (int32_t)(0.999 * (1 << FA_A));   /* folded at compile time */

    for (int l = 0; l < M_L; l++) {
        for (int i = 0; i < M_D; i++)
            for (int j = 0; j < M_N; j++) state[i][j] = 0;

        for (int t = 0; t < M_T; t++) {
            int16_t nh[M_D], dtp[M_D], dt[M_D], Bt[M_N], Ct[M_N], yv[M_D], ov[M_D];

            layernorm(h_res[t], M_D, NORM_W[l], NORM_SH[l], NORM_B[l],
                      FA_RES, FA_NORM, nh);

            linear_i16(nh, M_D, M_D, DT_W[l], DT_B[l], DT_SH[l],
                       FA_NORM, FA_DTP, dtp);
            for (int o = 0; o < M_D; o++)
                dt[o] = sat16(rsr(softplus_dtp(dtp[o]), FA_DTP - FA_DT));

            linear_i16(nh, M_D, M_N, B_W[l], B_B[l], B_SH[l], FA_NORM, FA_BC, Bt);
            linear_i16(nh, M_D, M_N, C_W[l], C_B[l], C_SH[l], FA_NORM, FA_BC, Ct);

            const int16_t *A = A_Q[l];
            for (int dd = 0; dd < M_D; dd++) {
                int32_t dtx = rsr((int32_t)dt[dd] * (int32_t)nh[dd], s_dtx);
                if (dtx > 32767) dtx = 32767;
                if (dtx < -32768) dtx = -32768;

                int32_t acc = 0;
                for (int nn = 0; nn < M_N; nn++) {
                    int32_t dtA = rsr((int32_t)dt[dd] * (int32_t)A[dd * M_N + nn],
                                      FA_DT + A_SH[l] - FA_A);
                    int32_t a = ONE - dtA;
                    if (a < 0)    a = 0;
                    if (a > AMAX) a = AMAX;

                    int32_t bb = rsr(dtx * (int32_t)Bt[nn], s_b);
                    int32_t hv = rsr(a * (int32_t)state[dd][nn], FA_A) + bb;
                    state[dd][nn] = sat16(hv);

                    acc += rsr((int32_t)state[dd][nn] * (int32_t)Ct[nn], SP);
                }
                int32_t y = rsr(acc, FA_H + FA_BC - FA_Y - SP);
                y += rsr((int32_t)DSKIP[l][dd] * (int32_t)nh[dd],
                         FA_NORM + DSKIP_SH[l] - FA_Y);
                yv[dd] = sat16(y);
            }

            linear_i16(yv, M_D, M_D, O_W[l], O_B[l], O_SH[l], FA_Y, FA_RES, ov);

            /* Safe in place: index t is never read again in this layer. */
            for (int o = 0; o < M_D; o++)
                h_res[t][o] = sat16((int32_t)h_res[t][o] + (int32_t)ov[o]);
        }
    }

    /* Mean pool. Divide before scaling: `sum << FA` overflows int32 while the
     * sum alone does not. The rescale shift is negative here (FA_RES < FA_POOL). */
    int16_t pooled[M_D];
    for (int o = 0; o < M_D; o++) {
        int32_t s = 0;
        for (int t = 0; t < M_T; t++) s += h_res[t][o];
        pooled[o] = sat16(rsr(fdiv(s, M_T), FA_RES - FA_POOL));
    }

    int16_t lg[M_CLASSES];
    linear_i16(pooled, M_D, M_CLASSES, head_w, head_b, head_w_sh,
               FA_POOL, FA_LOGIT, lg);
    for (int c = 0; c < M_CLASSES; c++) logits[c] = lg[c];
}

int ssm_argmax(const int32_t *logits)
{
    int best = 0;
    for (int c = 1; c < M_CLASSES; c++)
        if (logits[c] > logits[best]) best = c;
    return best;
}
