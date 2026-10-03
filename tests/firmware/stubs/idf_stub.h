#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>

#define CONFIG_HRC_DEVICE_ID "esp32_01"
#define CONFIG_HRC_WIFI_SSID ""
#define CONFIG_HRC_WIFI_PASS ""
#define CONFIG_HRC_MQTT_URI "mqtt://192.0.2.5:1883"
#define CONFIG_LWIP_SNTP_MAX_SERVERS 3
#define CONFIG_ESP_WIFI_ENABLE_WPA3_SAE 1
#define WPA3_SAE_PWE_BOTH 3
#define BIT0 1u
#define BIT1 2u
#define BIT2 4u
#define BIT3 8u
#define pdTRUE 1
#define pdFALSE 0
#define portMAX_DELAY UINT32_MAX
#define pdMS_TO_TICKS(x) (x)
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NO_MEM 0x101
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_NOT_FOUND 0x105
#define ESP_ERR_TIMEOUT 0x107
#define ESP_ERR_NVS_NO_FREE_PAGES 0x110d
#define ESP_ERR_NVS_NEW_VERSION_FOUND 0x1110
const char *esp_err_to_name(esp_err_t err);
void stub_log(const char *format, ...);
#define ESP_LOGI(tag,...) do { (void)(tag); stub_log(__VA_ARGS__); } while (0)
#define ESP_LOGW(tag,...) do { (void)(tag); stub_log(__VA_ARGS__); } while (0)
#define ESP_LOGE(tag,...) do { (void)(tag); stub_log(__VA_ARGS__); } while (0)
#define ESP_LOGD(tag,...) do { (void)(tag); if (0) stub_log(__VA_ARGS__); } while (0)

typedef uint32_t EventBits_t;
typedef struct { unsigned bits; } *EventGroupHandle_t;
typedef struct { unsigned taken; } *SemaphoreHandle_t;
EventGroupHandle_t xEventGroupCreate(void);
EventBits_t xEventGroupClearBits(EventGroupHandle_t, EventBits_t);
EventBits_t xEventGroupSetBits(EventGroupHandle_t, EventBits_t);
EventBits_t xEventGroupWaitBits(EventGroupHandle_t, EventBits_t, int, int, unsigned);
SemaphoreHandle_t xSemaphoreCreateMutex(void);
int xSemaphoreTake(SemaphoreHandle_t, unsigned);
int xSemaphoreGive(SemaphoreHandle_t);
typedef struct { void (*callback)(void *); void *arg; const char *name; } esp_timer_create_args_t;
typedef struct { esp_timer_create_args_t args; uint64_t delay; } *esp_timer_handle_t;
esp_err_t esp_timer_create(const esp_timer_create_args_t *, esp_timer_handle_t *);
esp_err_t esp_timer_stop(esp_timer_handle_t);
esp_err_t esp_timer_start_once(esp_timer_handle_t, uint64_t);

typedef const char *esp_event_base_t;
extern const char stub_wifi_event[], stub_ip_event[];
#define WIFI_EVENT stub_wifi_event
#define IP_EVENT stub_ip_event
#define ESP_EVENT_ANY_ID -1
#define WIFI_EVENT_STA_START 1
#define WIFI_EVENT_STA_DISCONNECTED 2
#define WIFI_EVENT_STA_STOP 3
#define IP_EVENT_STA_GOT_IP 4
#define IP_EVENT_STA_LOST_IP 5
typedef void (*event_callback_t)(void *, esp_event_base_t, int32_t, void *);
esp_err_t esp_event_loop_create_default(void);
esp_err_t esp_event_handler_register(esp_event_base_t, int32_t, event_callback_t, void *);
typedef struct { uint32_t addr; } ip4_t;
typedef struct { ip4_t ip, gw; } esp_netif_ip_info_t;
typedef struct { esp_netif_ip_info_t ip_info; } ip_event_got_ip_t;
typedef struct { int unused; } esp_netif_t;
#define IPSTR "%u.%u.%u.%u"
#define IP2STR(p) (unsigned)((p)->addr&255), (unsigned)(((p)->addr>>8)&255), (unsigned)(((p)->addr>>16)&255), (unsigned)(((p)->addr>>24)&255)
esp_err_t esp_netif_init(void);
esp_netif_t *esp_netif_create_default_wifi_sta(void);
esp_err_t esp_netif_get_ip_info(esp_netif_t *, esp_netif_ip_info_t *);

typedef struct { int unused; } wifi_init_config_t;
#define WIFI_INIT_CONFIG_DEFAULT() ((wifi_init_config_t){0})
#define WIFI_MODE_STA 1
#define WIFI_STORAGE_RAM 1
#define WIFI_IF_STA 0
#define WIFI_PS_NONE 0
enum { WIFI_AUTH_OPEN, WIFI_AUTH_WPA2_PSK, WIFI_AUTH_WPA_WPA2_PSK, WIFI_AUTH_WPA3_PSK, WIFI_AUTH_WPA2_WPA3_PSK };
#define WIFI_REASON_AUTH_FAIL 202
#define WIFI_REASON_NO_AP_FOUND 201
#define WIFI_REASON_HANDSHAKE_TIMEOUT 204
#define WIFI_REASON_BEACON_TIMEOUT 200
typedef struct { uint8_t reason; } wifi_event_sta_disconnected_t;
typedef struct { struct { uint8_t ssid[32], password[64]; struct { int authmode; } threshold; struct { bool capable; } pmf_cfg; int sae_pwe_h2e; } sta; } wifi_config_t;
typedef struct { bool show_hidden; } wifi_scan_config_t;
typedef struct { uint8_t ssid[33]; int8_t rssi; uint8_t primary; int authmode; } wifi_ap_record_t;
esp_err_t esp_wifi_init(const wifi_init_config_t *);
esp_err_t esp_wifi_set_storage(int);
esp_err_t esp_wifi_set_mode(int);
esp_err_t esp_wifi_set_ps(int);
esp_err_t esp_wifi_set_config(int, const wifi_config_t *);
esp_err_t esp_wifi_start(void);
esp_err_t esp_wifi_stop(void);
esp_err_t esp_wifi_connect(void);
esp_err_t esp_wifi_scan_start(const wifi_scan_config_t *, bool);
esp_err_t esp_wifi_scan_get_ap_records(uint16_t *, wifi_ap_record_t *);
esp_err_t esp_wifi_scan_get_ap_num(uint16_t *);
esp_err_t esp_wifi_scan_get_ap_record(wifi_ap_record_t *);
esp_err_t esp_wifi_clear_ap_list(void);

struct timeval;
#define ESP_SNTP_OPMODE_POLL 1
bool esp_sntp_enabled(void);
void esp_sntp_setoperatingmode(int);
void esp_sntp_set_time_sync_notification_cb(void (*)(struct timeval *));
void esp_sntp_setservername(int, const char *);
void esp_sntp_init(void);

typedef int nvs_handle_t;
#define NVS_READONLY 0
#define NVS_READWRITE 1
esp_err_t nvs_flash_init(void);
esp_err_t nvs_flash_erase(void);
esp_err_t nvs_open(const char *, int, nvs_handle_t *);
esp_err_t nvs_set_blob(nvs_handle_t, const char *, const void *, size_t);
esp_err_t nvs_get_blob(nvs_handle_t, const char *, void *, size_t *);
esp_err_t nvs_commit(nvs_handle_t);
void nvs_close(nvs_handle_t);

typedef struct queue_stub *QueueHandle_t;
QueueHandle_t xQueueCreate(unsigned, unsigned);
int xQueueSend(QueueHandle_t, const void *, unsigned);
int xQueueReceive(QueueHandle_t, void *, unsigned);
int xQueuePeek(QueueHandle_t, void *, unsigned);
unsigned uxQueueMessagesWaiting(QueueHandle_t);
unsigned uxQueueSpacesAvailable(QueueHandle_t);

typedef struct mqtt_stub *esp_mqtt_client_handle_t;
typedef struct {
    struct { struct { const char *uri; } address; } broker;
    struct { int keepalive, message_retransmit_timeout; } session;
    struct { int reconnect_timeout_ms, timeout_ms; } network;
    struct { int limit; } outbox;
} esp_mqtt_client_config_t;
typedef struct { int error_type, esp_transport_sock_errno; } mqtt_error_t;
typedef struct {
    esp_mqtt_client_handle_t client;
    char *topic, *data;
    int topic_len, data_len, current_data_offset, total_data_len, msg_id;
    mqtt_error_t *error_handle;
} esp_mqtt_event_t;
typedef esp_mqtt_event_t *esp_mqtt_event_handle_t;
typedef enum { MQTT_EVENT_CONNECTED=10, MQTT_EVENT_DISCONNECTED, MQTT_EVENT_DATA,
               MQTT_EVENT_SUBSCRIBED, MQTT_EVENT_PUBLISHED, MQTT_EVENT_ERROR } esp_mqtt_event_id_t;
#define MQTT_EVENT_ANY -1
esp_mqtt_client_handle_t esp_mqtt_client_init(const esp_mqtt_client_config_t *);
esp_err_t esp_mqtt_client_register_event(esp_mqtt_client_handle_t, int, event_callback_t, void *);
esp_err_t esp_mqtt_client_destroy(esp_mqtt_client_handle_t);
esp_err_t esp_mqtt_client_start(esp_mqtt_client_handle_t);
esp_err_t esp_mqtt_client_stop(esp_mqtt_client_handle_t);
esp_err_t esp_mqtt_client_disconnect(esp_mqtt_client_handle_t);
esp_err_t esp_mqtt_client_set_uri(esp_mqtt_client_handle_t, const char *);
int esp_mqtt_client_subscribe(esp_mqtt_client_handle_t, const char *, int);
int esp_mqtt_client_publish(esp_mqtt_client_handle_t, const char *, const char *, int, int, int);
int esp_mqtt_client_enqueue(esp_mqtt_client_handle_t, const char *, const char *, int, int, int, int);
int esp_mqtt_client_get_outbox_size(esp_mqtt_client_handle_t);

/* RMT/GPIO seam used by the HC-SR04 regression harness. */
#define IRAM_ATTR
#define GPIO_MODE_OUTPUT 1
#define GPIO_MODE_INPUT 0
#define GPIO_PULLUP_DISABLE 0
#define GPIO_PULLDOWN_DISABLE 0
#define GPIO_INTR_DISABLE 0
#define CONFIG_HRC_TRIG_GPIO 4
#define CONFIG_HRC_ECHO_GPIO 5
#define CONFIG_HRC_ECHO_TIMEOUT_MS 30
#define CONFIG_HRC_SOUND_SPEED_M_S 343
#define CONFIG_HRC_SAMPLE_PERIOD_MS 100
#define RMT_CLK_SRC_DEFAULT 0
typedef int gpio_num_t;
typedef struct { uint64_t pin_bit_mask; int mode,pull_up_en,pull_down_en,intr_type; } gpio_config_t;
esp_err_t gpio_config(const gpio_config_t *);
esp_err_t gpio_set_level(gpio_num_t, unsigned);
int gpio_get_level(gpio_num_t);
typedef struct { uint16_t duration0,duration1; unsigned level0,level1; } rmt_symbol_word_t;
typedef struct rmt_stub *rmt_channel_handle_t;
typedef struct { size_t num_symbols; rmt_symbol_word_t *received_symbols; } rmt_rx_done_event_data_t;
typedef struct { int gpio_num,clk_src; unsigned resolution_hz,mem_block_symbols; } rmt_rx_channel_config_t;
typedef struct { bool (*on_recv_done)(rmt_channel_handle_t,const rmt_rx_done_event_data_t *,void *); } rmt_rx_event_callbacks_t;
typedef struct { unsigned signal_range_min_ns,signal_range_max_ns; } rmt_receive_config_t;
esp_err_t rmt_new_rx_channel(const rmt_rx_channel_config_t *, rmt_channel_handle_t *);
esp_err_t rmt_rx_register_event_callbacks(rmt_channel_handle_t,const rmt_rx_event_callbacks_t *,void *);
esp_err_t rmt_enable(rmt_channel_handle_t);
esp_err_t rmt_disable(rmt_channel_handle_t);
esp_err_t rmt_del_channel(rmt_channel_handle_t);
esp_err_t rmt_receive(rmt_channel_handle_t,void *,size_t,const rmt_receive_config_t *);
SemaphoreHandle_t xSemaphoreCreateBinary(void);
void vSemaphoreDelete(SemaphoreHandle_t);
typedef int BaseType_t;
typedef unsigned TickType_t;
int xSemaphoreGiveFromISR(SemaphoreHandle_t, BaseType_t *);
int64_t esp_timer_get_time(void);
void esp_rom_delay_us(unsigned);
