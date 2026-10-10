#pragma once

/* Copy to network_config.local.h, then fill in SSID/password for each row.
 * Column 0 = SSID, column 1 = password (empty for an open network).
 * Add/remove rows as needed; empty SSID rows are skipped. Max 16 rows.
 * ESP32 scans, tries the strongest matching network, then the next candidate.
 * Rebuild/flash after editing. This mode ignores saved network settings. */
#define HRC_WIFI_LIST_MODE 1
#define HRC_WIFI_NETWORKS { \
    {"", ""}, \
    {"", ""}, \
}

/* Host LAN address and the MQTT port exposed by this repository's Docker. */
#define HRC_MQTT_URI "mqtt://192.168.100.3:1885"
