#include "lstm_inference.h"

#include <assert.h>
#include <inttypes.h>
#include <math.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const float k_input[10] = {
    0.180f, 0.195f, 0.210f, 0.225f, 0.240f,
    0.255f, 0.270f, 0.285f, 0.300f, 0.315f,
};

static float sliding_distance(uint32_t index)
{
    if (index < 10u)
    {
        return k_input[index];
    }
    return index == 10u ? 0.330f : 0.345f;
}

static void push_input(void)
{
    for (uint32_t i = 0; i < 10u; ++i)
    {
        const lstm_inference_status_t status =
            lstm_inference_push(i + 1u, (int64_t)(i + 1u) * 100000LL,
                                k_input[i], true);
        assert(status == (i < 9u ? LSTM_INFERENCE_STATUS_WARMUP
                                 : LSTM_INFERENCE_STATUS_OK));
    }
}

static void print_vector(void)
{
    const lstm_inference_result_t *result = lstm_inference_latest();
    printf("status=%s sample_count=%u threshold=%.9g predicted=%s%u early=%d\n",
           lstm_inference_status_name(result->status), result->sample_count,
           result->threshold_m, result->has_predicted_violation ? "" : "null",
           result->has_predicted_violation ? result->predicted_violation_ms : 0u,
           result->early_warning ? 1 : 0);
    printf("forecast=");
    for (size_t i = 0; i < LSTM_INFERENCE_OUTPUTS; ++i)
    {
        printf("%s%.9g", i == 0 ? "" : ",", result->distance_m[i]);
    }
    putchar('\n');
}

static void test_warmup(void)
{
    lstm_inference_init();
    assert(lstm_inference_status() == LSTM_INFERENCE_STATUS_WARMUP);
    assert(lstm_inference_push(1u, 100000LL, k_input[0], true) ==
           LSTM_INFERENCE_STATUS_WARMUP);
    assert(lstm_inference_latest()->sample_count == 1u);
}

static void test_invalid_reset(void)
{
    lstm_inference_init();
    push_input();
    assert(lstm_inference_push(11u, 1100000LL, NAN, false) ==
           LSTM_INFERENCE_STATUS_INVALID_WINDOW);
    assert(lstm_inference_latest()->sample_count == 0u);
    assert(lstm_inference_push(12u, 1200000LL, k_input[0], true) ==
           LSTM_INFERENCE_STATUS_WARMUP);
    assert(lstm_inference_latest()->sample_count == 1u);
}

static void test_gap_reset(void)
{
    lstm_inference_init();
    push_input();
    assert(lstm_inference_push(12u, 1200000LL, 0.33f, true) ==
           LSTM_INFERENCE_STATUS_INVALID_WINDOW);
    assert(lstm_inference_latest()->sample_count == 1u);
    assert(lstm_inference_push(13u, 1300000LL, 0.33f, true) ==
           LSTM_INFERENCE_STATUS_WARMUP);
    assert(lstm_inference_latest()->sample_count == 2u);
}

static void test_sliding(void)
{
    lstm_inference_init();
    for (uint32_t i = 0; i < 12u; ++i)
    {
        const lstm_inference_status_t status =
            lstm_inference_push(i + 1u, (int64_t)(i + 1u) * 100000LL,
                                sliding_distance(i), true);
        assert(status == (i < 9u ? LSTM_INFERENCE_STATUS_WARMUP
                                 : LSTM_INFERENCE_STATUS_OK));
    }
    assert(lstm_inference_latest()->sample_count == 10u);
    print_vector();
}

static void test_threshold_semantics(void)
{
    lstm_inference_init();
    assert(lstm_inference_set_threshold(0.040f));
    push_input();
    const lstm_inference_result_t *result = lstm_inference_latest();
    assert(result->status == LSTM_INFERENCE_STATUS_OK);
    assert(result->threshold_m == 0.040f);
    assert(result->early_warning ==
           (k_input[9] > result->threshold_m && result->has_predicted_violation));
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    if (strcmp(argv[1], "vector") == 0)
    {
        lstm_inference_init();
        push_input();
        print_vector();
        return 0;
    }
    if (strcmp(argv[1], "warmup") == 0)
    {
        test_warmup();
        return 0;
    }
    if (strcmp(argv[1], "invalid") == 0)
    {
        test_invalid_reset();
        return 0;
    }
    if (strcmp(argv[1], "gap") == 0)
    {
        test_gap_reset();
        return 0;
    }
    if (strcmp(argv[1], "threshold") == 0)
    {
        test_threshold_semantics();
        return 0;
    }
    if (strcmp(argv[1], "sliding") == 0)
    {
        test_sliding();
        return 0;
    }
    fprintf(stderr, "unknown case: %s\n", argv[1]);
    return EXIT_FAILURE;
}
