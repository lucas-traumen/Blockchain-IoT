/* Wi-Fi scan/serial provisioning, NVS settings, retry backoff and MQTT. */
#include "wifi_mqtt.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
#include <sys/time.h>
#include <time.h>
#include "network_config.h"
#include <limits.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_sntp.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "mqtt_client.h"
#include "nvs.h"
#include "nvs_flash.h"

static const char *TAG_WIFI = "hrc_wifi";
static const char *TAG_SNTP = "hrc_sntp";
static const char *TAG_MQTT = "hrc_mqtt";
#define WIFI_CONNECT_TIMEOUT_MS 15000
#define WIFI_HAS_IP BIT0
#define WIFI_STOPPED BIT1
#define WIFI_ATTEMPT_FAILED BIT2
#define WIFI_SELECTION_DONE BIT3
#define MQTT_CONTROL_TOPIC_PREFIX "hrc/v1/devices/"
#define MQTT_CONTROL_TOPIC_SUFFIX "/control"
static char s_topic_control[sizeof(MQTT_CONTROL_TOPIC_PREFIX) - 1 +
                            sizeof(CONFIG_HRC_DEVICE_ID) +
                            sizeof(MQTT_CONTROL_TOPIC_SUFFIX)];
typedef struct {
    char ssid[33];
    char password[65];
} wifi_credentials_t;
static wifi_credentials_t s_credentials;
static EventGroupHandle_t s_wifi_events;
static SemaphoreHandle_t s_wifi_mutex, s_mqtt_mutex;
static esp_netif_t *s_sta_netif;
static esp_timer_handle_t s_retry_timer;
static bool s_wifi_inited, s_wifi_started;
static atomic_bool s_wifi_connected, s_auto_connect, s_time_synced;
static atomic_uint s_retry_count;
static _Atomic(esp_mqtt_client_handle_t) s_mqtt_client;
static atomic_bool s_mqtt_connected, s_mqtt_requested;
static atomic_bool s_mqtt_started; /* writes protected by s_mqtt_mutex */
#define MQTT_OUTBOX_LIMIT 32768
#define MQTT_CONTROL_RESERVE 2048
#define MQTT_SUBSCRIBE_RESERVE 256
#define MQTT_TELEMETRY_LIMIT (MQTT_OUTBOX_LIMIT - MQTT_CONTROL_RESERVE)
#define MQTT_TX_TOPIC_SIZE 96
#define MQTT_TX_PAYLOAD_SIZE 384
#define MQTT_TX_QUEUE_LEN 16
#define MQTT_ACK_QUEUE_LEN 4
#define MQTT_TX_PERIOD_MS 100

typedef struct {
    char topic[MQTT_TX_TOPIC_SIZE];
    char payload[MQTT_TX_PAYLOAD_SIZE];
    int len;
} mqtt_tx_message_t;
static QueueHandle_t s_mqtt_tx_queue, s_mqtt_ack_queue;
static bool s_mqtt_worker_started; /* protected by s_mqtt_mutex */
static atomic_int s_mqtt_outbox_bytes, s_control_sub_id;
static atomic_bool s_control_ready, s_control_sub_pending;
static atomic_uint s_mqtt_connection_epoch, s_publish_acks;
static char s_broker_uri[HRC_BROKER_URI_MAX];
static hrc_control_cb_t s_control_cb;
static char *s_ctrl_buf;
static int s_ctrl_len;

#if HRC_WIFI_LIST_MODE
/* A two-dimensional array: each row contains {SSID, password}. */
static const char *const s_known_wifi[][2] = HRC_WIFI_NETWORKS;
#define KNOWN_WIFI_COUNT (sizeof(s_known_wifi) / sizeof(s_known_wifi[0]))
_Static_assert(KNOWN_WIFI_COUNT > 0 && KNOWN_WIFI_COUNT <= 16, "Wi-Fi list must contain 1..16 rows");
static atomic_bool s_wifi_selecting;
static atomic_int s_selection_result;
static esp_err_t start_wifi_selection(void);
static bool known_wifi_configured(void)
{
    for (size_t i = 0; i < KNOWN_WIFI_COUNT; i++)
        if (hrc_wifi_credentials_valid(s_known_wifi[i][0], s_known_wifi[i][1])) return true;
    return false;
}
#endif

bool hrc_wifi_uses_list(void) { return HRC_WIFI_LIST_MODE != 0; }

static void sntp_sync_cb(struct timeval *tv)
{
    s_time_synced = true;
    ESP_LOGI(TAG_SNTP, "time synced (epoch=%lld s)", (long long)tv->tv_sec);
}
static void sntp_start(void)
{
    if (esp_sntp_enabled()) return;
    setenv("TZ", "UTC", 1);
    tzset();
    esp_sntp_setoperatingmode(ESP_SNTP_OPMODE_POLL);
    esp_sntp_set_time_sync_notification_cb(sntp_sync_cb);
    esp_sntp_setservername(0, "pool.ntp.org");
#if CONFIG_LWIP_SNTP_MAX_SERVERS > 1
    esp_sntp_setservername(1, "time.google.com");
#endif
#if CONFIG_LWIP_SNTP_MAX_SERVERS > 2
    esp_sntp_setservername(2, "time.cloudflare.com");
#endif
    esp_sntp_init();
    ESP_LOGI(TAG_SNTP, "SNTP started, TZ=UTC");
}
static void schedule_wifi_retry(void)
{
    if (s_wifi_connected) return;
#if HRC_WIFI_LIST_MODE
    if (!known_wifi_configured()) return;
#else
    if (!s_auto_connect) return;
#endif
    unsigned retries = atomic_fetch_add(&s_retry_count, 1);
    uint32_t delay_ms = retries < 5 ? (1000u << retries) : 30000u;
    (void)esp_timer_stop(s_retry_timer);
    esp_err_t err = esp_timer_start_once(s_retry_timer, (uint64_t)delay_ms * 1000);
    if (err != ESP_OK) ESP_LOGE(TAG_WIFI, "retry timer: %s", esp_err_to_name(err));
    else ESP_LOGW(TAG_WIFI, "retry in %lu ms; %s",
                  (unsigned long)delay_ms, hrc_wifi_uses_list() ?
                  "rescan configured Wi-Fi list" : "use 'wifi' / 'join N' to change network");
}
static void wifi_retry_cb(void *arg)
{
    (void)arg;
#if HRC_WIFI_LIST_MODE
    if (!s_wifi_connected && start_wifi_selection() != ESP_OK) schedule_wifi_retry();
#else
    if (s_auto_connect && !s_wifi_connected && esp_wifi_connect() != ESP_OK)
        schedule_wifi_retry();
#endif
}
static const char *wifi_reason(uint8_t reason)
{
    switch (reason) {
    case WIFI_REASON_AUTH_FAIL: return "authentication failed";
    case WIFI_REASON_NO_AP_FOUND: return "AP not found (ESP32 uses 2.4 GHz)";
    case WIFI_REASON_HANDSHAKE_TIMEOUT: return "handshake timeout / check password";
    case WIFI_REASON_BEACON_TIMEOUT: return "AP beacon timeout";
    default: return "connection interrupted";
    }
}
static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        if (s_auto_connect && esp_wifi_connect() != ESP_OK) {
#if HRC_WIFI_LIST_MODE
            xEventGroupSetBits(s_wifi_events, WIFI_ATTEMPT_FAILED);
#else
            schedule_wifi_retry();
#endif
        }
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_wifi_connected = false;
        xEventGroupClearBits(s_wifi_events, WIFI_HAS_IP);
        if (s_auto_connect) {
            const wifi_event_sta_disconnected_t *ev = data;
            ESP_LOGW(TAG_WIFI, "disconnected, reason=%u (%s)", ev->reason, wifi_reason(ev->reason));
#if HRC_WIFI_LIST_MODE
            xEventGroupSetBits(s_wifi_events, WIFI_ATTEMPT_FAILED);
            if (!s_wifi_selecting) schedule_wifi_retry();
#else
            schedule_wifi_retry();
#endif
        }
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_STOP) {
        s_wifi_connected = false;
        xEventGroupClearBits(s_wifi_events, WIFI_HAS_IP);
        xEventGroupSetBits(s_wifi_events, WIFI_STOPPED);
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *ev = data;
        s_wifi_connected = true;
        s_retry_count = 0;
        (void)esp_timer_stop(s_retry_timer);
        ESP_LOGI(TAG_WIFI, "got IP: " IPSTR " gateway: " IPSTR,
                 IP2STR(&ev->ip_info.ip), IP2STR(&ev->ip_info.gw));
        sntp_start();
        xEventGroupSetBits(s_wifi_events, WIFI_HAS_IP);
        if (s_mqtt_requested) (void)hrc_mqtt_start();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_LOST_IP) {
        s_wifi_connected = false;
        xEventGroupClearBits(s_wifi_events, WIFI_HAS_IP);
    }
}
static esp_err_t save_setting(const char *key, const void *value, size_t size)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open("hrc_net", NVS_READWRITE, &handle);
    if (err != ESP_OK) return err;
    err = nvs_set_blob(handle, key, value, size);
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    if (err != ESP_OK) ESP_LOGE(TAG_WIFI, "NVS save failed: %s", esp_err_to_name(err));
    return err;
}
static void load_settings(void)
{
    memset(&s_credentials, 0, sizeof(s_credentials));
    if (hrc_broker_uri_valid(HRC_MQTT_URI)) strcpy(s_broker_uri, HRC_MQTT_URI);
#if !HRC_WIFI_LIST_MODE
    nvs_handle_t handle;
    if (hrc_wifi_credentials_valid(CONFIG_HRC_WIFI_SSID, CONFIG_HRC_WIFI_PASS)) {
        memcpy(s_credentials.ssid, CONFIG_HRC_WIFI_SSID, strlen(CONFIG_HRC_WIFI_SSID));
        memcpy(s_credentials.password, CONFIG_HRC_WIFI_PASS, strlen(CONFIG_HRC_WIFI_PASS));
    }
    if (nvs_open("hrc_net", NVS_READONLY, &handle) != ESP_OK) return;
    wifi_credentials_t stored = {0};
    size_t size = sizeof(stored);
    if (nvs_get_blob(handle, "wifi", &stored, &size) == ESP_OK && size == sizeof(stored) &&
        memchr(stored.ssid, 0, sizeof(stored.ssid)) &&
        memchr(stored.password, 0, sizeof(stored.password)) &&
        hrc_wifi_credentials_valid(stored.ssid, stored.password)) s_credentials = stored;
    char broker[HRC_BROKER_URI_MAX] = {0};
    size = sizeof(broker);
    if (nvs_get_blob(handle, "broker", broker, &size) == ESP_OK &&
        memchr(broker, 0, sizeof(broker)) && hrc_broker_uri_valid(broker)) strcpy(s_broker_uri, broker);
    nvs_close(handle);
    memset(&stored, 0, sizeof(stored));
#endif
}
/* Called under s_wifi_mutex. Wait for STA_STOP to drain previous attempt events. */
static esp_err_t wifi_pause(void)
{
    s_auto_connect = false;
    (void)esp_timer_stop(s_retry_timer);
    s_wifi_connected = false;
    xEventGroupClearBits(s_wifi_events, WIFI_HAS_IP | WIFI_STOPPED | WIFI_ATTEMPT_FAILED);
    if (!s_wifi_started) return ESP_OK;
    esp_err_t err = esp_wifi_stop();
    if (err != ESP_OK) return err;
    s_wifi_started = false;
    EventBits_t bits = xEventGroupWaitBits(s_wifi_events, WIFI_STOPPED, pdTRUE, pdTRUE,
                                           pdMS_TO_TICKS(2500));
    return (bits & WIFI_STOPPED) ? ESP_OK : ESP_ERR_TIMEOUT;
}
static esp_err_t wifi_resume(const wifi_credentials_t *credentials, bool connect)
{
    wifi_config_t cfg = {0};
    if (credentials->ssid[0]) {
        memcpy(cfg.sta.ssid, credentials->ssid, strlen(credentials->ssid));
        memcpy(cfg.sta.password, credentials->password, strlen(credentials->password));
        cfg.sta.threshold.authmode = credentials->password[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
        cfg.sta.pmf_cfg.capable = true;
        cfg.sta.sae_pwe_h2e = WPA3_SAE_PWE_BOTH;
        esp_err_t err = esp_wifi_set_config(WIFI_IF_STA, &cfg);
        memset(&cfg, 0, sizeof(cfg));
        if (err != ESP_OK) return err;
    }
#if !HRC_WIFI_LIST_MODE
    s_retry_count = 0;
#endif
    s_auto_connect = connect && credentials->ssid[0];
    esp_err_t err = esp_wifi_start();
    if (err == ESP_OK) s_wifi_started = true;
    return err;
}

#if HRC_WIFI_LIST_MODE
static bool usable_known_ap(const wifi_ap_record_t *record, const char *password)
{
    if (record->authmode == WIFI_AUTH_OPEN) return password[0] == 0;
    if (!password[0]) return false;
    switch (record->authmode) {
    case WIFI_AUTH_WPA2_PSK:
    case WIFI_AUTH_WPA_WPA2_PSK:
    case WIFI_AUTH_WPA2_WPA3_PSK: return true;
#if CONFIG_ESP_WIFI_ENABLE_WPA3_SAE
    case WIFI_AUTH_WPA3_PSK: return true;
#endif
    default: return false;
    }
}

/* Scan the complete SDK list one record at a time, so known weak APs are
 * considered even when more than 20 unrelated networks are nearby. */
static esp_err_t scan_known_wifi(bool seen[KNOWN_WIFI_COUNT], int rssi[KNOWN_WIFI_COUNT])
{
    wifi_credentials_t blank = {0};
    esp_err_t err = wifi_pause();
    if (err == ESP_OK) err = wifi_resume(&blank, false);
    const wifi_scan_config_t scan = {.show_hidden = false};
    if (err == ESP_OK) err = esp_wifi_scan_start(&scan, true);
    uint16_t total = 0;
    if (err == ESP_OK) err = esp_wifi_scan_get_ap_num(&total);
    if (err == ESP_OK) ESP_LOGI(TAG_WIFI, "Wi-Fi scan: %u APs, %u configured rows",
                               (unsigned)total, (unsigned)KNOWN_WIFI_COUNT);
    for (uint16_t n = 0; err == ESP_OK && n < total; n++) {
        wifi_ap_record_t record;
        err = esp_wifi_scan_get_ap_record(&record);
        if (err != ESP_OK) break;
        record.ssid[32] = 0;
        char display[33];
        memcpy(display, record.ssid, sizeof(display));
        for (size_t j = 0; display[j]; j++)
            if ((unsigned char)display[j] < 32 || display[j] == 127) display[j] = '?';
        ESP_LOGI(TAG_WIFI, "  %u. %s  %d dBm  ch %u", (unsigned)n + 1, display,
                 (int)record.rssi, (unsigned)record.primary);
        for (size_t i = 0; i < KNOWN_WIFI_COUNT; i++) {
            if (!hrc_wifi_credentials_valid(s_known_wifi[i][0], s_known_wifi[i][1]) ||
                strcmp(s_known_wifi[i][0], (const char *)record.ssid) ||
                !usable_known_ap(&record, s_known_wifi[i][1])) continue;
            if (!seen[i] || record.rssi > rssi[i]) rssi[i] = record.rssi;
            seen[i] = true;
        }
    }
    (void)esp_wifi_clear_ap_list();
    return err;
}

/* Runs in a task, never in the event loop or esp_timer callback. */
static void wifi_selection_task(void *arg)
{
    (void)arg;
    xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
    bool seen[KNOWN_WIFI_COUNT] = {0}, tried[KNOWN_WIFI_COUNT] = {0};
    int rssi[KNOWN_WIFI_COUNT] = {0};
    esp_err_t result = scan_known_wifi(seen, rssi);
    if (result == ESP_OK) result = ESP_ERR_NOT_FOUND;
    else {
        memset(seen, 0, sizeof(seen));
        ESP_LOGW(TAG_WIFI, "scan failed: %s", esp_err_to_name(result));
    }
    for (size_t attempts = 0; attempts < KNOWN_WIFI_COUNT; attempts++) {
        size_t best = KNOWN_WIFI_COUNT;
        int strongest = INT_MIN;
        for (size_t i = 0; i < KNOWN_WIFI_COUNT; i++) {
            if (seen[i] && !tried[i] && rssi[i] > strongest) {
                best = i;
                strongest = rssi[i];
            }
        }
        if (best == KNOWN_WIFI_COUNT) break;
        tried[best] = true;
        wifi_credentials_t next = {0};
        memcpy(next.ssid, s_known_wifi[best][0], strlen(s_known_wifi[best][0]));
        memcpy(next.password, s_known_wifi[best][1], strlen(s_known_wifi[best][1]));
        ESP_LOGI(TAG_WIFI, "connecting configured row %u: %s (%d dBm)",
                 (unsigned)best + 1, next.ssid, strongest);
        result = wifi_pause();
        if (result == ESP_OK) {
            s_credentials = next;
            result = wifi_resume(&next, true);
        }
        memset(&next, 0, sizeof(next));
        if (result == ESP_OK) {
            EventBits_t bits = xEventGroupWaitBits(s_wifi_events, WIFI_HAS_IP | WIFI_ATTEMPT_FAILED,
                                                  pdFALSE, pdFALSE,
                                                  pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));
            result = (bits & WIFI_HAS_IP) && s_wifi_connected ? ESP_OK : ESP_ERR_TIMEOUT;
        }
        if (result == ESP_OK) break;
        ESP_LOGW(TAG_WIFI, "configured row %u failed; trying next network", (unsigned)best + 1);
    }
    if (!s_wifi_connected) {
        wifi_credentials_t blank = {0};
        (void)wifi_pause();
        (void)wifi_resume(&blank, false);
        if (result == ESP_OK) result = ESP_ERR_TIMEOUT;
        ESP_LOGW(TAG_WIFI, "no configured network connected; edit network_config.local.h");
    }
    s_selection_result = result;
    xSemaphoreGive(s_wifi_mutex);
    s_wifi_selecting = false;
    if (!s_wifi_connected) schedule_wifi_retry();
    xEventGroupSetBits(s_wifi_events, WIFI_SELECTION_DONE);
    vTaskDelete(NULL);
}

static esp_err_t start_wifi_selection(void)
{
    bool expected = false;
    if (!atomic_compare_exchange_strong(&s_wifi_selecting, &expected, true)) return ESP_OK;
    for (size_t i = 0; i < KNOWN_WIFI_COUNT; i++) {
        const char *ssid = s_known_wifi[i][0];
        if ((!ssid || ssid[0]) && !hrc_wifi_credentials_valid(ssid, s_known_wifi[i][1]))
            ESP_LOGW(TAG_WIFI, "configured row %u invalid; check SSID/password length", (unsigned)i + 1);
    }
    xEventGroupClearBits(s_wifi_events, WIFI_SELECTION_DONE);
    if (xTaskCreate(wifi_selection_task, "wifi_list", 8192, NULL, 4, NULL) != pdPASS) {
        s_wifi_selecting = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
#endif

esp_err_t hrc_net_init(void)
{
    if (s_wifi_inited) return s_wifi_connected ? ESP_OK : ESP_ERR_INVALID_STATE;
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        err = nvs_flash_erase();
        if (err == ESP_OK) err = nvs_flash_init();
    }
    if (err != ESP_OK) return err;
    load_settings();
    s_wifi_events = xEventGroupCreate();
    s_wifi_mutex = xSemaphoreCreateMutex();
    s_mqtt_mutex = xSemaphoreCreateMutex();
    if (!s_wifi_events || !s_wifi_mutex || !s_mqtt_mutex) return ESP_ERR_NO_MEM;
    const esp_timer_create_args_t timer = {.callback=wifi_retry_cb, .name="wifi_retry"};
    if ((err = esp_timer_create(&timer, &s_retry_timer)) != ESP_OK) return err;
    if ((err = esp_netif_init()) != ESP_OK) return err;
    if ((err = esp_event_loop_create_default()) != ESP_OK) return err;
    s_sta_netif = esp_netif_create_default_wifi_sta();
    if (!s_sta_netif) return ESP_FAIL;
    const wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    if ((err = esp_wifi_init(&init)) != ESP_OK) return err;
    if ((err = esp_wifi_set_storage(WIFI_STORAGE_RAM)) != ESP_OK) return err;
    if ((err = esp_wifi_set_mode(WIFI_MODE_STA)) != ESP_OK) return err;
    if ((err = esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL)) != ESP_OK) return err;
    if ((err = esp_event_handler_register(IP_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, NULL)) != ESP_OK) return err;
    if ((err = wifi_resume(&s_credentials, !hrc_wifi_uses_list())) != ESP_OK) return err;
    s_wifi_inited = true;
    (void)esp_wifi_set_ps(WIFI_PS_NONE);
#if HRC_WIFI_LIST_MODE
    if ((err = start_wifi_selection()) != ESP_OK) {
        schedule_wifi_retry();
        return err;
    }
    EventBits_t bits = xEventGroupWaitBits(s_wifi_events, WIFI_HAS_IP | WIFI_SELECTION_DONE,
                                          pdFALSE, pdFALSE, pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));
    if ((bits & WIFI_HAS_IP) && s_wifi_connected) return ESP_OK;
    return (bits & WIFI_SELECTION_DONE) ? (esp_err_t)s_selection_result : ESP_ERR_TIMEOUT;
#else
    if (!s_credentials.ssid[0]) {
        ESP_LOGW(TAG_WIFI, "Wi-Fi not configured. Serial setup will list available networks.");
        return ESP_ERR_NOT_FOUND;
    }
    EventBits_t bits = xEventGroupWaitBits(s_wifi_events, WIFI_HAS_IP, pdFALSE, pdTRUE,
                                           pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));
    return (bits & WIFI_HAS_IP) ? ESP_OK : ESP_ERR_TIMEOUT;
#endif
}

bool hrc_wifi_connected(void) { return s_wifi_connected; }
bool hrc_wifi_configured(void)
{
#if HRC_WIFI_LIST_MODE
    return s_wifi_inited && known_wifi_configured();
#else
    return s_wifi_inited && s_credentials.ssid[0];
#endif
}

esp_err_t hrc_wifi_connect(const char *ssid, const char *password, bool *saved)
{
    if (saved) *saved = false;
    if (!s_wifi_inited) return ESP_ERR_INVALID_STATE;
    if (!hrc_wifi_credentials_valid(ssid, password)) return ESP_ERR_INVALID_ARG;
    xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
    wifi_credentials_t previous = s_credentials, next = {0};
    memcpy(next.ssid, ssid, strlen(ssid));
    memcpy(next.password, password, strlen(password));
    esp_err_t err = wifi_pause();
    if (err == ESP_OK) err = wifi_resume(&next, true);
    if (err == ESP_OK) {
        EventBits_t bits = xEventGroupWaitBits(s_wifi_events, WIFI_HAS_IP, pdFALSE, pdTRUE,
                                               pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));
        if (!(bits & WIFI_HAS_IP)) err = ESP_ERR_TIMEOUT;
    }
    if (err == ESP_OK) {
        s_credentials = next;
        esp_err_t stored = save_setting("wifi", &next, sizeof(next));
        if (saved) *saved = stored == ESP_OK;
    } else {
        (void)wifi_pause();
        (void)wifi_resume(&previous, true);
    }
    memset(&previous, 0, sizeof(previous));
    memset(&next, 0, sizeof(next));
    xSemaphoreGive(s_wifi_mutex);
    return err;
}

esp_err_t hrc_wifi_scan(hrc_setup_ap_t *aps, size_t capacity, size_t *count)
{
    if (!s_wifi_inited) return ESP_ERR_INVALID_STATE;
    if (!aps || !count || !capacity || capacity > HRC_SETUP_MAX_APS) return ESP_ERR_INVALID_ARG;
    *count = 0;
    xSemaphoreTake(s_wifi_mutex, portMAX_DELAY);
    bool paused = !s_wifi_connected;
    esp_err_t err = ESP_OK;
    if (paused) {
        err = wifi_pause();
        if (err == ESP_OK) err = wifi_resume(&s_credentials, false);
    }
    const wifi_scan_config_t scan = {.show_hidden=false};
    if (err == ESP_OK) err = esp_wifi_scan_start(&scan, true);
    wifi_ap_record_t records[HRC_SETUP_MAX_APS];
    uint16_t total = (uint16_t)capacity;
    if (err == ESP_OK) err = esp_wifi_scan_get_ap_records(&total, records);
    if (err == ESP_OK) {
        for (uint16_t i = 0; i < total; i++) {
            hrc_setup_ap_t ap = {0};
            memcpy(ap.ssid, records[i].ssid, 32);
            ap.rssi = records[i].rssi;
            ap.channel = records[i].primary;
            switch (records[i].authmode) {
            case WIFI_AUTH_OPEN: ap.security="OPEN"; ap.supported=true; break;
            case WIFI_AUTH_WPA2_PSK: ap.security="WPA2"; ap.supported=true; ap.needs_password=true; break;
            case WIFI_AUTH_WPA_WPA2_PSK: ap.security="WPA/WPA2"; ap.supported=true; ap.needs_password=true; break;
            case WIFI_AUTH_WPA3_PSK:
            case WIFI_AUTH_WPA2_WPA3_PSK:
                ap.security=records[i].authmode == WIFI_AUTH_WPA3_PSK ? "WPA3" : "WPA2/WPA3";
                ap.needs_password=true;
#if CONFIG_ESP_WIFI_ENABLE_WPA3_SAE
                ap.supported=true;
#else
                ap.supported=records[i].authmode == WIFI_AUTH_WPA2_WPA3_PSK;
#endif
                break;
            default: ap.security="enterprise/legacy"; break;
            }
            if (ap.ssid[0]) aps[(*count)++] = ap;
        }
    } else (void)esp_wifi_clear_ap_list();
    if (paused) {
        esp_err_t resume = wifi_pause();
        if (resume == ESP_OK) resume = wifi_resume(&s_credentials, true);
        if (err == ESP_OK) err = resume;
    }
    xSemaphoreGive(s_wifi_mutex);
    return err;
}

void hrc_network_status(char *out, size_t capacity)
{
    esp_netif_ip_info_t ip = {0};
    if (s_sta_netif && s_wifi_connected) (void)esp_netif_get_ip_info(s_sta_netif, &ip);
    if (s_mqtt_mutex) xSemaphoreTake(s_mqtt_mutex, portMAX_DELAY);
    snprintf(out, capacity, "wifi=%s ip=" IPSTR " mqtt=%s control=%d broker=%s pending=%u "
             "outbox=%d bytes pubacks=%u time_synced=%d\n",
             s_wifi_connected ? "connected" : "offline", IP2STR(&ip.ip),
             hrc_mqtt_connected() ? "connected" : "offline", (int)hrc_mqtt_control_ready(), s_broker_uri,
             hrc_mqtt_pending_count(), (int)s_mqtt_outbox_bytes, (unsigned)s_publish_acks, (int)s_time_synced);
    if (s_mqtt_mutex) xSemaphoreGive(s_mqtt_mutex);
}

bool hrc_time_synced(void)
{
    return s_time_synced;
}

int64_t hrc_time_utc_ms(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}

/* ---------------------------------------------------------------------- */
/* MQTT                                                                   */
/* ---------------------------------------------------------------------- */

void hrc_mqtt_on_control(hrc_control_cb_t cb)
{
    s_control_cb = cb;
}

/* Payload control gom đủ fragment, null-terminate rồi mới gọi callback. */
static void handle_mqtt_data(const esp_mqtt_event_handle_t event)
{
    /* Chỉ nhận topic control. Fragment ĐẦU (current_data_offset == 0) là
     * nơi duy nhất mang topic pointer — fragment 2..N của message dài chỉ
     * có data + offset (esp-mqtt không set topic ở các event sau), nên lọc
     * topic ở fragment đầu; fragment sau ghép thẳng theo offset (s_ctrl_buf
     * khác NULL nghĩa là đang gom một message đã qua lọc topic). */
    if (event->current_data_offset == 0)
    {
        const size_t want = strlen(s_topic_control);
        if (event->topic == NULL ||
            event->topic_len != (int)want ||
            memcmp(event->topic, s_topic_control, want) != 0)
        {
            ESP_LOGW(TAG_MQTT, "DATA trên topic lạ (len=%d) — bỏ qua",
                     event->topic_len);
            return;
        }
    }
    if (event->total_data_len <= 0)
    {
        return;
    }

    /* Chunk đầu tiên (message nằm trọn trong 1 event cũng đi qua đây). */
    if (event->current_data_offset == 0)
    {
        char *buf = malloc((size_t)event->total_data_len + 1);
        if (buf == NULL)
        {
            ESP_LOGE(TAG_MQTT, "malloc %d byte cho payload control thất bại",
                     event->total_data_len);
            return;
        }
        free(s_ctrl_buf);
        s_ctrl_buf = buf;
        s_ctrl_len = 0;
    }
    if (s_ctrl_buf == NULL)
    {
        return; /* malloc chunk đầu đã fail — bỏ qua toàn bộ message */
    }

    if (s_ctrl_len + event->data_len > event->total_data_len)
    {
        ESP_LOGE(TAG_MQTT, "fragmentation payload không nhất quán — bỏ qua");
        free(s_ctrl_buf);
        s_ctrl_buf = NULL;
        s_ctrl_len = 0;
        return;
    }
    memcpy(s_ctrl_buf + s_ctrl_len, event->data, event->data_len);
    s_ctrl_len += event->data_len;
    if (s_ctrl_len < event->total_data_len)
    {
        return; /* còn chunk tiếp theo */
    }

    /* Đủ message: null-terminate trước khi callback (len không tính '\0'). */
    s_ctrl_buf[s_ctrl_len] = '\0';
    if (s_control_cb != NULL)
    {
        s_control_cb(s_topic_control, s_ctrl_buf, s_ctrl_len);
    }
    free(s_ctrl_buf);
    s_ctrl_buf = NULL;
    s_ctrl_len = 0;
}

static void mqtt_event_handler(void *handler_args, esp_event_base_t base,
                               int32_t event_id, void *event_data)
{
    (void)handler_args;
    (void)base;
    const esp_mqtt_event_handle_t event = (esp_mqtt_event_handle_t)event_data;

    switch ((esp_mqtt_event_id_t)event_id)
    {
    case MQTT_EVENT_CONNECTED:
        s_control_ready = false;
        s_control_sub_pending = false;
        s_control_sub_id = -1;
        s_mqtt_connection_epoch++;
        s_mqtt_connected = true;
        ESP_LOGI(TAG_MQTT, "connected to %s; control subscription pending", s_broker_uri);
        break;

    case MQTT_EVENT_DISCONNECTED:
        s_mqtt_connected = false;
        s_control_ready = false;
        s_control_sub_pending = false;
        s_control_sub_id = -1;
        ESP_LOGW(TAG_MQTT, "MQTT disconnected; automatic reconnect in 5 s");
        break;

    case MQTT_EVENT_DATA:
        handle_mqtt_data(event);
        break;

    case MQTT_EVENT_SUBSCRIBED: {
        int pending_id = s_control_sub_id;
        if (!s_control_sub_pending || (pending_id > 0 && event->msg_id != pending_id)) break;
        s_control_sub_pending = false;
        if (event->data && event->data_len == 1 && (uint8_t)event->data[0] == 1) {
            s_control_ready = true;
            ESP_LOGI(TAG_MQTT, "control ready (QoS1, msg_id=%d)", event->msg_id);
        } else {
            s_control_ready = false;
            ESP_LOGW(TAG_MQTT, "control SUBACK rejected or QoS1 not granted; retrying");
        }
        break;
    }

    case MQTT_EVENT_PUBLISHED:
        s_publish_acks++;
        ESP_LOGD(TAG_MQTT, "publish ack (msg_id=%d)", event->msg_id);
        break;

    case MQTT_EVENT_ERROR:
        if (event->error_handle != NULL)
        {
            ESP_LOGE(TAG_MQTT, "MQTT error type=%d socket_errno=%d; check broker host/port and LAN route",
                     event->error_handle->error_type, event->error_handle->esp_transport_sock_errno);
        }
        else
        {
            ESP_LOGE(TAG_MQTT, "MQTT error (không có error_handle)");
        }
        break;

    default:
        break;
    }
}

/* Network writes and subscription attempts run in this worker. Producers copy to bounded
 * mailboxes with zero wait, so a stalled socket cannot pause distance sampling.
 * Control subscription and session ACKs have priority over telemetry. */
static void mqtt_tx_task(void *arg)
{
    (void)arg;
    static unsigned epoch;
    static int64_t subscribe_due_us, subscribe_deadline_us, error_log_us;
    static bool backpressure;
    static unsigned ack_snapshot;
    static int64_t publish_progress_us;
    for (;;) {
        xSemaphoreTake(s_mqtt_mutex, portMAX_DELAY);
        esp_mqtt_client_handle_t client = s_mqtt_client;
        if (client && s_mqtt_started) {
            int64_t now = esp_timer_get_time();
            unsigned connection_epoch = s_mqtt_connection_epoch;
            if (epoch != connection_epoch) {
                epoch = connection_epoch;
                subscribe_due_us = subscribe_deadline_us = 0;
                if (publish_progress_us) publish_progress_us = now;
            }
            int bytes = esp_mqtt_client_get_outbox_size(client);
            s_mqtt_outbox_bytes = bytes;
            now = esp_timer_get_time();
            unsigned pubacks = s_publish_acks;
            if (!bytes) publish_progress_us = 0;
            if (pubacks != ack_snapshot) {
                ack_snapshot = pubacks;
                if (publish_progress_us) publish_progress_us = now;
            }
            if (hrc_mqtt_connected() && publish_progress_us &&
                now - publish_progress_us >= 15000000) {
                ESP_LOGW(TAG_MQTT, "no PUBACK progress for 15 s (outbox=%d); requesting reconnect", bytes);
                if (esp_mqtt_client_disconnect(client) == ESP_OK) {
                    s_mqtt_connected = false;
                    s_control_ready = false;
                }
                publish_progress_us = now;
            }
            if (hrc_mqtt_connected()) {
                if (s_control_sub_pending && now >= subscribe_deadline_us)
                    s_control_sub_pending = false;
                if (!s_control_ready && !s_control_sub_pending && now >= subscribe_due_us) {
                    /* 0 allows a SUBACK racing the subscribe return to be accepted. */
                    s_control_sub_id = 0;
                    s_control_sub_pending = true;
                    subscribe_deadline_us = now + 15000000;
                    int id = esp_mqtt_client_subscribe(client, s_topic_control, 1);
                    subscribe_due_us = esp_timer_get_time() + 1000000;
                    if (id < 0) {
                        s_control_sub_pending = false;
                        s_control_sub_id = -1;
                        ESP_LOGW(TAG_MQTT, "control subscribe failed (code=%d outbox=%d); retry in 1 s",
                                 id, bytes);
                    } else {
                        s_control_sub_id = id;
                    }
                    bytes = esp_mqtt_client_get_outbox_size(client);
                }
                if (backpressure && bytes <= MQTT_TELEMETRY_LIMIT / 2) {
                    backpressure = false;
                    ESP_LOGI(TAG_MQTT, "telemetry transfer resumed (outbox=%d bytes, pubacks=%u)",
                             bytes, (unsigned)s_publish_acks);
                }
                for (unsigned sent = 0; sent < 4; sent++) {
                    mqtt_tx_message_t message;
                    QueueHandle_t queue = s_mqtt_ack_queue;
                    bool ack = xQueuePeek(queue, &message, 0) == pdTRUE;
                    if (!ack) {
                        if (!s_control_ready || backpressure) break;
                        queue = s_mqtt_tx_queue;
                        if (xQueuePeek(queue, &message, 0) != pdTRUE) break;
                    }
                    /* Include maximum MQTT fixed header, topic length and packet ID.
                     * esp-mqtt's own limit check accounts only for payload bytes. */
                    int packet_bytes = message.len + (int)strlen(message.topic) + 9;
                    int budget = ack ? MQTT_OUTBOX_LIMIT - MQTT_SUBSCRIBE_RESERVE : MQTT_TELEMETRY_LIMIT;
                    if (bytes + packet_bytes > budget) {
                        if (!ack && !backpressure) {
                            backpressure = true;
                            ESP_LOGW(TAG_MQTT, "telemetry transfer paused (outbox=%d bytes, pubacks=%u); "
                                              "waiting for broker ACKs, control reserve=%d bytes",
                                     bytes, (unsigned)s_publish_acks, MQTT_CONTROL_RESERVE);
                        }
                        break;
                    }
                    int id = esp_mqtt_client_enqueue(client, message.topic, message.payload,
                                                    message.len, 1, 0, true);
                    if (id < 0) {
                        if (!error_log_us || now - error_log_us >= 5000000) {
                            ESP_LOGW(TAG_MQTT, "MQTT transfer blocked (code=%d outbox=%d); retaining queued message",
                                     id, bytes);
                            error_log_us = now;
                        }
                        break;
                    }
                    (void)xQueueReceive(queue, &message, 0);
                    if (!publish_progress_us) publish_progress_us = esp_timer_get_time();
                    bytes = esp_mqtt_client_get_outbox_size(client);
                }
            }
            s_mqtt_outbox_bytes = bytes;
        }
        xSemaphoreGive(s_mqtt_mutex);
        vTaskDelay(pdMS_TO_TICKS(MQTT_TX_PERIOD_MS));
    }
}

static esp_err_t mqtt_tx_start(void)
{
    if (s_mqtt_worker_started) return ESP_OK;
    if (!s_mqtt_tx_queue)
        s_mqtt_tx_queue = xQueueCreate(MQTT_TX_QUEUE_LEN, sizeof(mqtt_tx_message_t));
    if (!s_mqtt_ack_queue)
        s_mqtt_ack_queue = xQueueCreate(MQTT_ACK_QUEUE_LEN, sizeof(mqtt_tx_message_t));
    if (!s_mqtt_tx_queue || !s_mqtt_ack_queue) return ESP_ERR_NO_MEM;
    if (xTaskCreate(mqtt_tx_task, "mqtt_tx", 4096, NULL, 4, NULL) != pdPASS)
        return ESP_ERR_NO_MEM;
    s_mqtt_worker_started = true;
    return ESP_OK;
}

esp_err_t hrc_mqtt_start(void)
{
    s_mqtt_requested = true;
    if (s_mqtt_started) return ESP_OK;
    if (!s_mqtt_mutex) return ESP_ERR_INVALID_STATE;
    xSemaphoreTake(s_mqtt_mutex, portMAX_DELAY);
    esp_err_t err = mqtt_tx_start();
    if (err != ESP_OK) {
        xSemaphoreGive(s_mqtt_mutex);
        return err;
    }
    if (!s_wifi_connected || s_mqtt_started) {
        xSemaphoreGive(s_mqtt_mutex);
        return ESP_OK; /* deferred until GOT_IP, or already started */
    }
    err = ESP_OK;
    if (!hrc_broker_uri_valid(s_broker_uri)) err = ESP_ERR_INVALID_ARG;
    if (err == ESP_OK && !s_mqtt_client) {
        snprintf(s_topic_control, sizeof(s_topic_control),
                 MQTT_CONTROL_TOPIC_PREFIX "%s" MQTT_CONTROL_TOPIC_SUFFIX, CONFIG_HRC_DEVICE_ID);
        const esp_mqtt_client_config_t cfg = {
            .broker.address.uri=s_broker_uri,
            .session.keepalive=20,
            .session.message_retransmit_timeout=5000,
            .network.reconnect_timeout_ms=5000,
            .network.timeout_ms=10000,
            .outbox.limit=MQTT_OUTBOX_LIMIT,
        };
        esp_mqtt_client_handle_t client = esp_mqtt_client_init(&cfg);
        if (!client) err = ESP_ERR_NO_MEM;
        else {
            err = esp_mqtt_client_register_event(client, MQTT_EVENT_ANY, mqtt_event_handler, NULL);
            if (err != ESP_OK) esp_mqtt_client_destroy(client);
            else s_mqtt_client = client;
        }
    }
    if (err == ESP_OK) {
        err = esp_mqtt_client_start(s_mqtt_client);
        s_mqtt_started = err == ESP_OK;
    }
    xSemaphoreGive(s_mqtt_mutex);
    if (err != ESP_OK) ESP_LOGE(TAG_MQTT, "start failed: %s; configure 'broker mqtt://HOST:PORT'", esp_err_to_name(err));
    return err;
}

esp_err_t hrc_mqtt_set_broker(const char *uri)
{
    if (!hrc_broker_uri_valid(uri)) return ESP_ERR_INVALID_ARG;
    if (!s_mqtt_mutex) return ESP_ERR_INVALID_STATE;
    xSemaphoreTake(s_mqtt_mutex, portMAX_DELAY);
    esp_err_t err = ESP_OK;
    if (s_mqtt_started) {
        err = esp_mqtt_client_stop(s_mqtt_client);
        if (err == ESP_OK) s_mqtt_started = false;
    }
    if (err == ESP_OK && s_mqtt_client) err = esp_mqtt_client_set_uri(s_mqtt_client, uri);
    if (err == ESP_OK) {
        strcpy(s_broker_uri, uri);
        s_mqtt_connected = false;
        s_control_ready = false;
        s_control_sub_pending = false;
        err = save_setting("broker", s_broker_uri, strlen(s_broker_uri) + 1);
    }
    xSemaphoreGive(s_mqtt_mutex);
    if (s_mqtt_requested) (void)hrc_mqtt_start();
    return err;
}

int hrc_mqtt_outbox_size(void) { return s_mqtt_outbox_bytes; }
bool hrc_mqtt_control_ready(void) { return hrc_mqtt_connected() && s_control_ready; }
unsigned hrc_mqtt_pending_count(void)
{
    return (s_mqtt_tx_queue ? uxQueueMessagesWaiting(s_mqtt_tx_queue) : 0) +
           (s_mqtt_ack_queue ? uxQueueMessagesWaiting(s_mqtt_ack_queue) : 0);
}
bool hrc_mqtt_connected(void) { return s_mqtt_connected && s_wifi_connected; }

static esp_err_t queue_mqtt_message(QueueHandle_t queue, const char *topic,
                                    const char *payload, int len)
{
    if (!queue || !s_mqtt_client) return ESP_ERR_INVALID_STATE;
    if (!topic || !topic[0] || !payload || len < 0 || len >= MQTT_TX_PAYLOAD_SIZE ||
        strlen(topic) >= MQTT_TX_TOPIC_SIZE) return ESP_ERR_INVALID_ARG;
    if (!len) len = (int)strlen(payload);
    if (len >= MQTT_TX_PAYLOAD_SIZE) return ESP_ERR_INVALID_ARG;
    mqtt_tx_message_t message = {.len = len};
    strcpy(message.topic, topic);
    memcpy(message.payload, payload, (size_t)len);
    return xQueueSend(queue, &message, 0) == pdTRUE ? ESP_OK : ESP_ERR_NO_MEM;
}

esp_err_t hrc_mqtt_publish(const char *topic, const char *payload, int len)
{
    return queue_mqtt_message(s_mqtt_ack_queue, topic, payload, len);
}
esp_err_t hrc_mqtt_enqueue(const char *topic, const char *payload, int len)
{
    return queue_mqtt_message(s_mqtt_tx_queue, topic, payload, len);
}
