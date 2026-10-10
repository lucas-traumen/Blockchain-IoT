#include "diagnostics.h"
#include <limits.h>

hrc_fault_action_t hrc_fault_update(hrc_fault_state_t *state, uint32_t code,
                                  int64_t now_us)
{
    if (code == 0) {
        hrc_fault_action_t result = state->code ? HRC_FAULT_RECOVERED : HRC_FAULT_QUIET;
        *state = (hrc_fault_state_t){0};
        return result;
    }
    const int changed = state->code != code;
    state->code = code;
    if (state->consecutive < UINT32_MAX) state->consecutive++;
    if (changed || now_us < state->reported_us || now_us - state->reported_us >= 5000000) {
        state->reported_us = now_us;
        return HRC_FAULT_REPORT;
    }
    return HRC_FAULT_QUIET;
}
