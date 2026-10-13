#pragma once
#include <stdbool.h>
#include "esp_err.h"

/* Uses the configured UART console. A single task owns input; passwords masked. */
esp_err_t hrc_network_console_start(bool initial_scan);
