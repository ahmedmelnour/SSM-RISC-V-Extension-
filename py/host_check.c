/* ---------------------------------------------------------------------------
 * host_check.c -- run fw/ssm_model.c natively and compare against the golden
 * vectors, without a board or a Vivado rebuild.
 *
 *   gcc -O2 -I fw -o /tmp/host_check py/host_check.c fw/ssm_model.c && /tmp/host_check
 *
 * The firmware image is baked into the BRAM initialisation, so every change to
 * the model code costs a full ~15 minute synthesis run. Debugging the integer
 * arithmetic that way would be miserable and slow. The C here is plain C99 with
 * no target dependency, so the same source that runs on CV32E40X can be checked
 * on the host in under a second -- and only a source that already matches the
 * Python reference is worth putting on the core.
 *
 * Passing here is necessary, not sufficient: the host is 64-bit little-endian
 * with different int promotion habits, so the on-hardware run is still the
 * thing the gate asks for. But every bug this catches is a bug that would
 * otherwise have cost a synthesis cycle to find.
 * --------------------------------------------------------------------------- */

#include <stdio.h>
#include <stdint.h>
#include "ssm_model.h"
#include "model_data.h"
#ifndef GOLDEN_HEADER
#define GOLDEN_HEADER "golden.h"
#endif
#include GOLDEN_HEADER

int main(void)
{
    int bad = 0, wrongclass = 0;

    printf("host check: %d golden beats, D=%d N=%d L=%d T=%d\n",
           G_BEATS, M_D, M_N, M_L, M_T);

    for (int b = 0; b < G_BEATS; b++) {
        int32_t logits[M_CLASSES];
        ssm_infer(&g_x[(size_t)b * M_T], logits);

        int mismatch = 0;
        for (int c = 0; c < M_CLASSES; c++)
            if (logits[c] != g_logits[(size_t)b * M_CLASSES + c]) mismatch = 1;

        int pred = ssm_argmax(logits);
        int gold = ssm_argmax(&g_logits[(size_t)b * M_CLASSES]);
        if (pred != gold) wrongclass++;

        if (mismatch || G_BEATS <= 16) {
            printf("  beat %d (label %u): %s   got [", b, g_label[b],
                   mismatch ? "MISMATCH" : "exact   ");
            for (int c = 0; c < M_CLASSES; c++) printf("%6d", logits[c]);
            printf(" ]\n");
        }
        if (mismatch) {
            printf("                       expected [");
            for (int c = 0; c < M_CLASSES; c++)
                printf("%6d", (int)g_logits[(size_t)b * M_CLASSES + c]);
            printf(" ]\n                       delta    [");
            for (int c = 0; c < M_CLASSES; c++)
                printf("%6d", (int)(logits[c] - g_logits[(size_t)b * M_CLASSES + c]));
            printf(" ]\n");
            bad++;
        }
    }

    printf("\n%d/%d bit-exact, %d class disagreements\n",
           G_BEATS - bad, G_BEATS, wrongclass);
    printf("%s\n", bad ? "FAIL" : "PASS");
    return bad ? 1 : 0;
}
