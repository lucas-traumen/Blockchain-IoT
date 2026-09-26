/*
 * HRC Safety Log — Wi-Fi STA + SNTP + MQTT client (ESP-IDF v5.x) — M4c
 *
 * Luồng khởi tạo (M4d sẽ nối trong app_main):
 *   1. hrc_mqtt_on_control(cb) — đăng ký callback trước (tùy chọn).
 *   2. hrc_net_init() — NVS → esp_netif → event loop → Wi-Fi STA →
 *      blocking chờ IP (15 s). SNTP khởi động tự động trong event
 *      IP_EVENT_STA_GOT_IP (TZ = UTC; pool.ntp.org + 2 server dự phòng),
 *      nên khi mất kết nối rồi lấy lại IP, sync giờ tự tiếp tục.
 *   3. hrc_mqtt_start() — tạo MQTT client tới CONFIG_HRC_MQTT_URI;
 *      MQTT_EVENT_CONNECTED → subscribe topic control QoS1.
 *   4. hrc_mqtt_publish()/hrc_mqtt_enqueue() — publish QoS1, retain=false;
 *      enqueue non-blocking dùng cho telemetry (không block task đo).
 *
 * Tự reconnect:
 *   - Wi-Fi: WIFI_EVENT_STA_DISCONNECTED → esp_wifi_connect() lại (vô hạn).
 *   - SNTP: lwIP tự truy vấn lại theo POLL interval.
 *   - MQTT: esp-mqtt tự reconnect mặc định (~10 s) khi mất TCP/MQTT.
 *
 * Payload lệnh control: esp-mqtt có thể chia message thành nhiều event
 * (MQTT_EVENT_DATA với current_data_offset). handle_mqtt_data() gom đủ các
 * fragment vào buffer heap, null-terminate rồi mới gọi callback.
 *
 * TAG log riêng: hrc_wifi / hrc_sntp / hrc_mqtt.
 */

#include "wifi_mqtt.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "sdkconfig.h"

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "esp_err.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_sntp.h"
#include "esp_wifi.h"

#include "mqtt_client.h"

#include "nvs_flash.h"

/* TAG log riêng cho từng lớp. */
static const char *TAG_WIFI = "hrc_wifi";
static const char *TAG_SNTP = "hrc_sntp";
static const char *TAG_MQTT = "hrc_mqtt";

/* Blocking chờ IP lần đầu trong hrc_net_init(). */
#define WIFI_CONNECT_TIMEOUT_MS 15000

/* Topic control: hrc/v1/devices/<CONFIG_HRC_DEVICE_ID>/control.
 * Kích thước buffer tính lúc build từ literal Kconfig nên không tràn:
 * prefix(15) + device_id(strlen) + suffix(8) + '\0'. */
#define MQTT_CONTROL_TOPIC_PREFIX "hrc/v1/devices/"
#define MQTT_CONTROL_TOPIC_SUFFIX "/control"
static char s_topic_control[sizeof(MQTT_CONTROL_TOPIC_PREFIX) - 1 +
                            sizeof(CONFIG_HRC_DEVICE_ID) +
                            sizeof(MQTT_CONTROL_TOPIC_SUFFIX)];

/* --- Trạng thái Wi-Fi / SNTP --- */
static SemaphoreHandle_t s_wifi_ip_sem;  /* give ở IP_EVENT_STA_GOT_IP */
static esp_netif_t *s_sta_netif;         /* NULL = chưa tạo */
static bool s_wifi_inited;               /* tài nguyên Wi-Fi đã cấp */
static volatile bool s_wifi_connected;   /* STA đang có IP */
static volatile bool s_time_synced;      /* SNTP đã sync >= 1 lần từ boot */

/* --- Trạng thái MQTT --- */
static esp_mqtt_client_handle_t s_mqtt_client; /* NULL = chưa start */
static volatile bool s_mqtt_connected;
static hrc_control_cb_t s_control_cb;    /* callback control (M4d đăng ký) */

/* Buffer gom payload control bị chia fragment giữa các MQTT_EVENT_DATA. */
static char *s_ctrl_buf;
static int s_ctrl_len;

/* ---------------------------------------------------------------------- */
/* SNTP                                                                   */
/* ---------------------------------------------------------------------- */

/* Callback sync: esp_sntp gọi khi nhận được giờ hợp lệ. */
static void sntp_sync_cb(struct timeval *tv)
{
    s_time_synced = true;
    ESP_LOGI(TAG_SNTP, "time synced (epoch=%lld s)", (long long)tv->tv_sec);
}

/*
 * Khởi động SNTP (idempotent — esp_sntp_enabled() chặn khởi động lại).
 * Gọi từ event IP_EVENT_STA_GOT_IP nên luôn chạy sau khi đã có IP và DNS.
 */
static void sntp_start(void)
{
#if CONFIG_LWIP_SNTP_MAX_SERVERS > 0
    if (esp_sntp_enabled())
    {
        return; /* đã chạy rồi */
    }

    /* Firmware làm việc toàn bộ theo UTC (measured_at là epoch UTC ms). */
    setenv("TZ", "UTC", 1);
    tzset();

    esp_sntp_setoperatingmode(ESP_SNTP_OPMODE_POLL);
    esp_sntp_set_time_sync_notification_cb(sntp_sync_cb);
    esp_sntp_setservername(0, "pool.ntp.org");
#if CONFIG_LWIP_SNTP_MAX_SERVERS > 1
    esp_sntp_setservername(1, "time.google.com");   /* dự phòng 1 */
#endif
#if CONFIG_LWIP_SNTP_MAX_SERVERS > 2
    esp_sntp_setservername(2, "time.cloudflare.com"); /* dự phòng 2 */
#endif
    esp_sntp_init();
    ESP_LOGI(TAG_SNTP, "SNTP started (pool.ntp.org + %d backup), TZ=UTC",
             (CONFIG_LWIP_SNTP_MAX_SERVERS > 1 ? 1 : 0) +
                 (CONFIG_LWIP_SNTP_MAX_SERVERS > 2 ? 1 : 0));
#else
    ESP_LOGE(TAG_SNTP, "SNTP bị tắt trong sdkconfig "
                       "(CONFIG_LWIP_SNTP_MAX_SERVERS=0) — không sync được giờ");
#endif
}

/* ---------------------------------------------------------------------- */
/* Wi-Fi                                                                  */
/* ---------------------------------------------------------------------- */

/* Handler event Wi-Fi/IP: connect lại khi mất STA, báo IP khi có. */
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT)
    {
        switch (event_id)
        {
        case WIFI_EVENT_STA_START:
            esp_wifi_connect();
            break;
        case WIFI_EVENT_STA_DISCONNECTED:
            s_wifi_connected = false;
            ESP_LOGW(TAG_WIFI, "STA disconnected — tự reconnect...");
            esp_wifi_connect(); /* tự reconnect vô hạn */
            break;
        default:
            break;
        }
    }
    else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP)
    {
        const ip_event_got_ip_t *ev = (const ip_event_got_ip_t *)event_data;
        s_wifi_connected = true;
        ESP_LOGI(TAG_WIFI, "got IP: " IPSTR, IP2STR(&ev->ip_info.ip));

        /* SNTP khởi động ngay khi lần đầu có IP (idempotent). */
        sntp_start();

        /* Đánh thức hrc_net_init() đang chờ (give là no-op khi sem đầy). */
        xSemaphoreGive(s_wifi_ip_sem);
    }
}

esp_err_t hrc_net_init(void)
{
    if (s_wifi_inited)
    {
        /* Đã gọi trước đó: không tạo lại tài nguyên, chỉ trả trạng thái. */
        ESP_LOGW(TAG_WIFI, "hrc_net_init gọi lại khi đã init");
        return s_wifi_connected ? ESP_OK : ESP_ERR_INVALID_STATE;
    }

    /* 1. NVS (Wi-Fi driver cần). Partition mới/hết trang → erase rồi init. */
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND)
    {
        ESP_LOGW(TAG_WIFI, "NVS cần erase — nvs_flash_erase()");
        err = nvs_flash_erase();
        if (err == ESP_OK)
        {
            err = nvs_flash_init();
        }
    }
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG_WIFI, "nvs_flash_init: %s", esp_err_to_name(err));
        return err;
    }

    /* 2. netif + event loop mặc định. */
    err = esp_netif_init();
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG_WIFI, "esp_netif_init: %s", esp_err_to_name(err));
        return err;
    }
    err = esp_event_loop_create_default();
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG_WIFI, "esp_event_loop_create_default: %s",
                 esp_err_to_name(err));
        return err;
    }

    /* 3. Semaphore chờ IP (tạo trước khi register event). */
    s_wifi_ip_sem = xSemaphoreCreateBinary();
    if (s_wifi_ip_sem == NULL)
    {
        return ESP_ERR_NO_MEM;
    }

    /* 4. Default STA netif + Wi-Fi driver. */
    s_sta_netif = esp_netif_create_default_wifi_sta();
    if (s_sta_netif == NULL)
    {
        return ESP_FAIL;
    }
    const wifi_init_config_t wifi_cfg = WIFI_INIT_CONFIG_DEFAULT();
    err = esp_wifi_init(&wifi_cfg);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG_WIFI, "esp_wifi_init: %s", esp_err_to_name(err));
        return err;
    }

    /* 5. Event handlers: mọi event Wi-Fi + event GOT_IP. */
    err = esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                     &wifi_event_handler, NULL);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG_WIFI, "register WIFI_EVENT handler: %s",
                 esp_err_to_name(err));
        return err;
    }
    err = esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                     &wifi_event_handler, NULL);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG_WIFI, "register IP_EVENT handler: %s",
                 esp_err_to_name(err));
        return err;
    }

    /* 6. Chế độ STA + SSID/PASS từ Kconfig. */
    err = esp_wifi_set_mode(WIFI_MODE_STA);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG_WIFI, "esp_wifi_set_mode: %s", esp_err_to_name(err));
        return err;
    }
    wifi_config_t sta_cfg = { 0 };
    strncpy((char *)sta_cfg.sta.ssid, CONFIG_HRC_WIFI_SSID,
            sizeof(sta_cfg.sta.ssid) - 1);
    sta_cfg.sta.ssid[sizeof(sta_cfg.sta.ssid) - 1] = '\0';
    strncpy((char *)sta_cfg.sta.password, CONFIG_HRC_WIFI_PASS,
            sizeof(sta_cfg.sta.password) - 1);
    sta_cfg.sta.password[sizeof(sta_cfg.sta.password) - 1] = '\0';
    /* Chỉ yêu cầu WPA2 khi có mật khẩu (chấp nhận cả AP mở khi PASS rỗng). */
    if (CONFIG_HRC_WIFI_PASS[0] != '\0')
    {
        sta_cfg.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    }
    err = esp_wifi_set_config(WIFI_IF_STA, &sta_cfg);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG_WIFI, "esp_wifi_set_config: %s", esp_err_to_name(err));
        return err;
    }

    /* Tắt power-save Wi-Fi: sample period 100 ms (M4d) cần latency ổn định;
     * default WIFI_PS_MIN_MODEM thêm ~100 ms waker delay giữa các publish. */
    esp_wifi_set_ps(WIFI_PS_NONE);

    /* 7. Start; STA_START event → esp_wifi_connect(). */
    err = esp_wifi_start();
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG_WIFI, "esp_wifi_start: %s", esp_err_to_name(err));
        return err;
    }
    s_wifi_inited = true; /* các lần gọi sau không tạo lại tài nguyên */

    /* 8. Blocking chờ IP lần đầu (SNTP tự start trong event GOT_IP). */
    if (xSemaphoreTake(s_wifi_ip_sem, pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS)) !=
        pdTRUE)
    {
        ESP_LOGE(TAG_WIFI, "không lấy được IP trong %d ms — Wi-Fi vẫn tự "
                           "reconnect nền, gọi lại hrc_net_init() để kiểm tra",
                 (int)WIFI_CONNECT_TIMEOUT_MS);
        return ESP_ERR_TIMEOUT;
    }

    ESP_LOGI(TAG_WIFI, "Wi-Fi STA connected: ssid=\"%s\"",
             CONFIG_HRC_WIFI_SSID);
    return ESP_OK;
}

/* ---------------------------------------------------------------------- */
/* Thời gian                                                              */
/* ---------------------------------------------------------------------- */

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
        s_mqtt_connected = true;
        ESP_LOGI(TAG_MQTT, "connected to %s", CONFIG_HRC_MQTT_URI);
        {
            const int msg_id =
                esp_mqtt_client_subscribe(event->client, s_topic_control, 1);
            if (msg_id < 0)
            {
                ESP_LOGE(TAG_MQTT, "subscribe control QoS1 thất bại");
            }
            else
            {
                ESP_LOGI(TAG_MQTT, "subscribed \"%s\" (QoS1, msg_id=%d)",
                         s_topic_control, msg_id);
            }
        }
        break;

    case MQTT_EVENT_DISCONNECTED:
        s_mqtt_connected = false;
        /* esp-mqtt tự reconnect (mặc định ~10 s). */
        ESP_LOGW(TAG_MQTT, "MQTT disconnected — esp-mqtt sẽ tự reconnect");
        break;

    case MQTT_EVENT_DATA:
        handle_mqtt_data(event);
        break;

    case MQTT_EVENT_SUBSCRIBED:
        ESP_LOGI(TAG_MQTT, "subscribe ack (msg_id=%d)", event->msg_id);
        break;

    case MQTT_EVENT_PUBLISHED:
        ESP_LOGD(TAG_MQTT, "publish ack (msg_id=%d)", event->msg_id);
        break;

    case MQTT_EVENT_ERROR:
        if (event->error_handle != NULL)
        {
            ESP_LOGE(TAG_MQTT, "MQTT error type=%d",
                     event->error_handle->error_type);
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

esp_err_t hrc_mqtt_start(void)
{
    if (s_mqtt_client != NULL)
    {
        ESP_LOGW(TAG_MQTT, "hrc_mqtt_start gọi lại khi đã start — bỏ qua");
        return ESP_OK;
    }

    /* Topic control (kích thước buffer đã tính lúc build, không tràn). */
    snprintf(s_topic_control, sizeof(s_topic_control),
             MQTT_CONTROL_TOPIC_PREFIX "%s" MQTT_CONTROL_TOPIC_SUFFIX,
             CONFIG_HRC_DEVICE_ID);

    const esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = CONFIG_HRC_MQTT_URI,
        /* Giới hạn dung lượng outbox (bytes, ESP-IDF v5.5: .outbox.limit)
         * để đứt mạng dài không phình heap vô hạn: ~40 mẫu × ~250 B ≈ 10 KB;
         * message vượt giới hạn bị esp-mqtt drop (enqueue trả -2). Message
         * nằm trong outbox quá CONFIG_MQTT_OUTBOX_EXPIRED_TIMEOUT_MS
         * (mặc định 30 s) tự bị xóa. */
        .outbox.limit = 10240,
    };
    s_mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    if (s_mqtt_client == NULL)
    {
        ESP_LOGE(TAG_MQTT, "esp_mqtt_client_init thất bại (uri=\"%s\")",
                 CONFIG_HRC_MQTT_URI);
        return ESP_ERR_INVALID_STATE;
    }

    esp_err_t err = esp_mqtt_client_register_event(
        s_mqtt_client, MQTT_EVENT_ANY, mqtt_event_handler, NULL);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG_MQTT, "register_event: %s", esp_err_to_name(err));
        esp_mqtt_client_destroy(s_mqtt_client);
        s_mqtt_client = NULL;
        return err;
    }

    /* esp-mqtt tự connect + tự reconnect khi mất kết nối. */
    err = esp_mqtt_client_start(s_mqtt_client);
    if (err != ESP_OK)
    {
        ESP_LOGE(TAG_MQTT, "client_start: %s", esp_err_to_name(err));
        esp_mqtt_client_destroy(s_mqtt_client);
        s_mqtt_client = NULL;
        return err;
    }

    ESP_LOGI(TAG_MQTT, "MQTT client started: uri=%s control=\"%s\"",
             CONFIG_HRC_MQTT_URI, s_topic_control);
    return ESP_OK;
}

bool hrc_mqtt_connected(void)
{
    return s_mqtt_connected;
}

esp_err_t hrc_mqtt_publish(const char *topic, const char *payload, int len)
{
    if (s_mqtt_client == NULL)
    {
        ESP_LOGE(TAG_MQTT, "publish trước hrc_mqtt_start()");
        return ESP_ERR_INVALID_STATE;
    }
    /* QoS1, retain=false. esp_mqtt_client_publish có thể gọi từ task khác
     * với task esp-mqtt (an toàn); trả msg_id >= 0 khi message đã nhận. */
    const int msg_id = esp_mqtt_client_publish(s_mqtt_client, topic, payload,
                                               len, 1, 0);
    if (msg_id < 0)
    {
        ESP_LOGE(TAG_MQTT, "publish \"%s\" (len=%d) thất bại (connected=%d)",
                 topic, len, (int)s_mqtt_connected);
        return ESP_FAIL;
    }
    ESP_LOGD(TAG_MQTT, "published \"%s\" (len=%d, msg_id=%d)", topic, len,
             msg_id);
    return ESP_OK;
}

esp_err_t hrc_mqtt_enqueue(const char *topic, const char *payload, int len)
{
    if (s_mqtt_client == NULL)
    {
        ESP_LOGE(TAG_MQTT, "enqueue trước hrc_mqtt_start()");
        return ESP_ERR_INVALID_STATE;
    }
    /* QoS1, retain=false, store=true: ghi message vào outbox và trả NGAY
     * (non-blocking) — task telemetry không bị chặn network timeout ~10 s
     * khi mạng xấu; task esp-mqtt gửi khi có thể, kể cả khi chưa connected
     * (gửi ngay khi kết nối được thiết lập lại). Outbox đã giới hạn bằng
     * .outbox.limit trong hrc_mqtt_start(). */
    const int msg_id = esp_mqtt_client_enqueue(s_mqtt_client, topic, payload,
                                               len, 1, 0, 1);
    if (msg_id < 0)
    {
        /* -1 = lỗi nội bộ, -2 = outbox đầy (vượt .outbox.limit). */
        ESP_LOGW(TAG_MQTT, "enqueue \"%s\" (len=%d) thất bại (connected=%d)",
                 topic, len, (int)s_mqtt_connected);
        return ESP_FAIL;
    }
    ESP_LOGD(TAG_MQTT, "enqueued \"%s\" (len=%d, msg_id=%d)", topic, len,
             msg_id);
    return ESP_OK;
}
