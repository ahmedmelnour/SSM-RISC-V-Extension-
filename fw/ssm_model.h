/* ---------------------------------------------------------------------------
 * ssm_model.h -- integer inference for the selective SSM.
 *
 * Must reproduce py/quantize.py exactly. See ssm_model.c for the three places
 * where the obvious C differs from the Python reference.
 * --------------------------------------------------------------------------- */

#ifndef SSM_MODEL_H
#define SSM_MODEL_H

#include <stdint.h>

/* x: M_T int16 samples, Q15 full-scale, exactly as stored in beats.npz.
 * logits: M_CLASSES int32 (int16-valued) at FA_LOGIT fractional bits. */
void ssm_infer(const int16_t *x, int32_t *logits);

int ssm_argmax(const int32_t *logits);

#endif /* SSM_MODEL_H */
