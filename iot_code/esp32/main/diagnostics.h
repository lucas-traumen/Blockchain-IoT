#pragma once
#include <stdint.h>

typedef struct {
    uint32_t code;
    uint32_t consecutive;
    int64_t reported_us;
} hrc_fault_state_t;

typedef enum {
    HRC_FAULT_QUIET,
    HRC_FAULT_REPORT,
    HRC_FAULT_RECOVERED,
} hrc_fault_action_t;

/* Each error transition and recovery is visible; repeated errors report at 5s. */
hrc_fault_action_t hrc_fault_update(hrc_fault_state_t *state, uint32_t code,
                                  int64_t now_us);
