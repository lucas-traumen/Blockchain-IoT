/*
 * HRC Safety Log — fixed float32 LSTM inference for ESP32 / ESP32-S3.
 *
 * The implementation is generated around the exact Keras artifact contract:
 * input (10, 2) -> LSTM(32, tanh/sigmoid) -> Dense(32, relu) -> Dense(5).
 * The model weights are stored in lstm_inference.c as static const float data;
 * no TensorFlow, pandas or heap allocation is required on the device.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define LSTM_INFERENCE_WINDOW_SIZE 10u
#define LSTM_INFERENCE_OUTPUTS 5u
#define LSTM_INFERENCE_HORIZON_STEP_MS 100u

typedef enum
{
    LSTM_INFERENCE_STATUS_WARMUP = 0,
    LSTM_INFERENCE_STATUS_OK,
    LSTM_INFERENCE_STATUS_INVALID_WINDOW,
    LSTM_INFERENCE_STATUS_MODEL_UNAVAILABLE,
    LSTM_INFERENCE_STATUS_FAULT,
} lstm_inference_status_t;

typedef struct
{
    lstm_inference_status_t status;
    uint8_t sample_count;
    float distance_m[LSTM_INFERENCE_OUTPUTS];
    float threshold_m;
    bool has_predicted_violation;
    uint16_t predicted_violation_ms;
    bool early_warning;
} lstm_inference_result_t;

/* Initialize/reset the fixed-size sample buffer and latest result. */
void lstm_inference_init(void);

/*
 * Add one telemetry sample.
 *
 * A sample must be valid and have a contiguous sequence and timestamp near
 * CONFIG_HRC_SAMPLE_PERIOD_MS (100 ms by default). Invalid samples, sequence
 * gaps and timing gaps reset the warmup window. A valid sample that exposes a
 * gap is retained as entry 1 and reports INVALID_WINDOW; the following sample
 * reports WARMUP with entry 2, matching the backend window contract.
 */
lstm_inference_status_t lstm_inference_push(uint32_t sequence,
                                             int64_t monotonic_us,
                                             float distance_m,
                                             bool valid);

/* Latest status/result; returned storage remains owned by this module. */
lstm_inference_status_t lstm_inference_status(void);
const lstm_inference_result_t *lstm_inference_latest(void);
const char *lstm_inference_status_name(lstm_inference_status_t status);

/*
 * Configure the physical violation threshold in metres. Returns false for a
 * non-finite or negative value. The Kconfig default is 40 mm (0.040 m).
 */
bool lstm_inference_set_threshold(float threshold_m);
float lstm_inference_threshold(void);

#ifdef __cplusplus
}
#endif
