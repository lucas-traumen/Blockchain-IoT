#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "network_setup.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef void (*hrc_control_cb_t)(const char *topic, const char *payload, int len);

/* List mode: scan and try configured SSID/password rows by RSSI; macros
 * override NVS. Retry rounds run in a task with 1..30s backoff.
 * Legacy mode: load NVS, then Kconfig fallback. Initial wait is at most 15s;
 * connection attempts may continue in the background. SNTP starts on GOT_IP. */
esp_err_t hrc_net_init(void);
bool hrc_wifi_connected(void);
bool hrc_wifi_uses_list(void);
bool hrc_wifi_configured(void);
esp_err_t hrc_wifi_scan(hrc_setup_ap_t *aps, size_t capacity, size_t *count);
/* Failed join restores previous runtime network and never replaces saved
 * credentials. Successful IP acquisition saves credentials in NVS. */
esp_err_t hrc_wifi_connect(const char *ssid, const char *password, bool *saved);
void hrc_network_status(char *out, size_t capacity);
bool hrc_time_synced(void);
int64_t hrc_time_utc_ms(void);

/* Callback runs in the MQTT task: enqueue control only, never block/publish. */
void hrc_mqtt_on_control(hrc_control_cb_t cb);
/* Deferred until Wi-Fi has IP. List mode uses HRC_MQTT_URI; legacy uses NVS/Kconfig. */
esp_err_t hrc_mqtt_start(void);
esp_err_t hrc_mqtt_set_broker(const char *uri);
bool hrc_mqtt_connected(void);
/* Cached SDK outbox bytes; querying this never waits for the network mutex. */
int hrc_mqtt_outbox_size(void);
unsigned hrc_mqtt_pending_count(void);
bool hrc_mqtt_control_ready(void);
/* QoS1, retain=false. Zero-wait RAM mailbox: ESP_OK means accepted for
 * transfer, not delivered. A worker prioritizes control subscription and ACKs,
 * reserves SDK outbox space, and waits for SUBACK before sending telemetry.
 * Returns NO_MEM for a full mailbox, INVALID_STATE before the first client,
 * INVALID_ARG for invalid/oversized topic or payload. Buffering is bounded;
 * messages still in RAM/SDK can be lost on power loss or SDK expiry. */
esp_err_t hrc_mqtt_publish(const char *topic, const char *payload, int len);
esp_err_t hrc_mqtt_enqueue(const char *topic, const char *payload, int len);

#ifdef __cplusplus
}
#endif
