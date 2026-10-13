/*
 * HRC Safety Log — payload telemetry_v1 + session control + app loop
 * (ESP-IDF v5.x) — M4d
 *
 * Nhiệm vụ của module:
 *   1. Mỗi chu kỳ CONFIG_HRC_SAMPLE_PERIOD_MS: đo hcsr04_measure_once()
 *      (M4b), quy đổi echo_us → distance_mm bằng CONFIG_HRC_SOUND_SPEED_M_S
 *      (số nguyên), phân loại valid/error_code, build payload ĐÚNG 11 field
 *      theo schemas/telemetry_v1.schema.json và enqueue publish QoS1
 *      (non-blocking) lên topic hrc/v1/devices/<device_id>/telemetry.
 *   2. Nhận lệnh control_v1: callback MQTT (chạy trong task esp-mqtt) chỉ
 *      parse tối thiểu và đẩy vào mailbox (queue depth 4); telemetry_task
 *      lấy ra, quyết định và build ack ĐÚNG 9 field theo
 *      schemas/ack_v1.schema.json, publish lên topic .../ack.
 *
 * Quy ước thời gian trong payload:
 *   - monotonic_us = r.start_monotonic_us (esp_timer chốt ngay TRƯỚC khi
 *     driver phát TRIG) — cùng mốc so sánh giữa các mẫu trong một boot.
 *   - measured_at đọc hrc_time_utc_ms() TRƯỚC khi gọi measure_once nên lệch
 *     so với mốc monotonic một khoảng nhỏ (thiết lập phiên nhận RMT, cỡ vài
 *     chục µs); timestamp chỉ tới ms nên chấp nhận được (ghi trong README).
 *   - Chưa sync SNTP → measured_at = null, time_synced = false. KHÔNG bao giờ
 *     thay bằng giờ khác (uptime hay giờ iot_backend).
 *
 * Lua chon edge case (chi tiết trong README):
 *   - echo hoàn tất nhưng distance > 4500 mm (ngoài miền schema): invalid,
 *     error_code "ECHO_TIMEOUT", distance_mm = null, echo_us GIỮ SỐ (không
 *     clamp số liệu đo được).
 *   - error_code khi driver lỗi: echo_us = (echo_us >= 1) ? số : null.
 *   - Ack khi chưa có mẫu nào (sequence nội bộ 0): dùng sequence = 1 vì
 *     schema yêu cầu minimum 1.
 *
 * Trạng thái chia sẻ (s_sequence, s_boot_id, topic): s_sequence chỉ được
 * đụng tới trong telemetry_task (sample + ack cùng một task) nên không cần
 * khóa; s_boot_id/topic ghi đúng một lần trong telemetry_init trước khi task
 * được tạo.
 */

#include "telemetry.h"

#include <inttypes.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include "sdkconfig.h"

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

#include "esp_err.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"

#include "hcsr04.h"
#include "wifi_mqtt.h"
#include "diagnostics.h"
#include "lstm_inference.h"

#ifndef CONFIG_HRC_DISTANCE_LOG_PERIOD_MS
/* Keep an old sdkconfig from silently compiling out the sensor diagnostics. */
#define CONFIG_HRC_DISTANCE_LOG_PERIOD_MS 1000
#endif

#ifndef CONFIG_HRC_LSTM_INFERENCE
/* Legacy native telemetry harnesses can compile without the model object. */
#define CONFIG_HRC_LSTM_INFERENCE 0
#endif

static const char *TAG = "telemetry";

/* Mailbox control: queue depth 4, phần tử struct bên dưới. */
#define HRC_CONTROL_QUEUE_LEN 4

/* session_id tối đa 63 ký tự + '\0' (đủ cho iot_backend; payload dài hơn bị từ
 * chối — không truncate để không trả ack với session_id bị cắt xén). */
#define HRC_SESSION_ID_LEN 64

/* Chuỗi command gốc, CHỈ dùng cho log warn (lệnh lạ KHÔNG được ack nên
 * không echo vào JSON ack — session_start/stop là 13/12 ký tự; dài hơn bị
 * từ chối ở bước parse — defensive, iot_backend hợp lệ không bao giờ gửi). */
#define HRC_CMD_RAW_LEN 24

/* boot_id: 16 ký tự hex + '\0'. */
#define HRC_BOOT_ID_LEN 17

/* Topic: "hrc/v1/devices/" (15) + device_id + "/telemetry" (10) hoặc "/ack"
 * (4). 96 byte chứa device_id tới ~66 ký tự; dài hơn bị log error lúc init. */
#define HRC_TOPIC_LEN 96

/* Buffer JSON: payload 11 field (device_id + boot_id 16 + measured_at 24 +
 * các số) rơi quanh ~200 byte; 384 là dư sức, >= 256 theo yêu cầu. */
#define HRC_JSON_BUF_LEN 384

/* "YYYY-MM-DDTHH:MM:SS.mmmZ" = 24 ký tự + '\0' → 32 là đủ. */
#define HRC_MEASURED_AT_LEN 32

/* Miền distance_mm của schemas/telemetry_v1.schema.json. */
#define HRC_MAX_DISTANCE_MM 4500

/* Lệnh control đã map từ chuỗi JSON. */
typedef enum
{
    HRC_CMD_SESSION_START = 0, /* "session_start" — mở phiên đo */
    HRC_CMD_SESSION_STOP,      /* "session_stop"  — đóng phiên đo */
    HRC_CMD_UNKNOWN,           /* lệnh lạ ngoài control_v1 (defensive) */
} hrc_command_t;

/* Phần tử mailbox control. command_raw giữ chuỗi gốc CHỈ để log warn
 * (không echo vào JSON ack — lệnh lạ không được ack). */
typedef struct
{
    hrc_command_t command;
    char command_raw[HRC_CMD_RAW_LEN];
    char session_id[HRC_SESSION_ID_LEN]; /* rỗng = payload thiếu session_id */
} hrc_control_msg_t;

static hrc_fault_state_t s_sensor_fault, s_enqueue_fault;
#if CONFIG_HRC_DISTANCE_LOG_PERIOD_MS > 0
static int64_t s_distance_log_us;
#endif
static atomic_uint s_samples, s_invalid_samples, s_dropped_samples;
static atomic_uint s_sensor_error;

#if CONFIG_HRC_LSTM_INFERENCE
static const char *telemetry_lstm_status_name(void)
{
    return lstm_inference_status_name(lstm_inference_status());
}
#endif

void telemetry_status(char *out, size_t capacity)
{
#if CONFIG_HRC_LSTM_INFERENCE
    snprintf(out, capacity, "sensor TRIG=GPIO%d ECHO=GPIO%d period=%d ms timeout=%d ms "
             "samples=%u invalid=%u enqueue_dropped=%u sensor_error=%u lstm=%s\n",
             CONFIG_HRC_TRIG_GPIO, CONFIG_HRC_ECHO_GPIO, CONFIG_HRC_SAMPLE_PERIOD_MS,
             CONFIG_HRC_ECHO_TIMEOUT_MS, (unsigned)s_samples, (unsigned)s_invalid_samples,
             (unsigned)s_dropped_samples, (unsigned)s_sensor_error,
             telemetry_lstm_status_name());
#else
    snprintf(out, capacity, "sensor TRIG=GPIO%d ECHO=GPIO%d period=%d ms timeout=%d ms "
             "samples=%u invalid=%u enqueue_dropped=%u sensor_error=%u\n",
             CONFIG_HRC_TRIG_GPIO, CONFIG_HRC_ECHO_GPIO, CONFIG_HRC_SAMPLE_PERIOD_MS,
             CONFIG_HRC_ECHO_TIMEOUT_MS, (unsigned)s_samples, (unsigned)s_invalid_samples,
             (unsigned)s_dropped_samples, (unsigned)s_sensor_error);
#endif
}

static QueueHandle_t s_control_queue; /* NULL = telemetry_init chưa chạy */
static uint32_t s_sequence;           /* số mẫu đã build; mẫu đầu tiên = 1 */
static char s_boot_id[HRC_BOOT_ID_LEN];
static char s_topic_telemetry[HRC_TOPIC_LEN];
static char s_topic_ack[HRC_TOPIC_LEN];

/*
 * Parser tay, bounded cho JSON phẳng của control_v1 (iot_backend luôn gửi hợp
 * lệ nên lenient là đủ): tìm khóa "key" (kèm dấu ") rồi đọc giá trị string
 * đến dấu " đóng, cho phép khoảng trắng quanh ':'. Không unescape — chuỗi
 * iot_backend hợp lệ không cần; session_id echo lại vào ack giữ nguyên dạng
 * escape nên JSON ack vẫn hợp lệ (lệnh lạ KHÔNG được ack — F7, chỉ log).
 * Trả 0 khi đọc được; -1 khi thiếu khóa/không phải string/giá
 * trị quá dài cho out (từ chối thay vì truncate — không ack dữ liệu cắt xén).
 */
static int telemetry_json_find_string(const char *json, const char *key,
                                      char *out, size_t out_size)
{
    char needle[64];
    if (snprintf(needle, sizeof(needle), "\"%s\"", key) >= (int)sizeof(needle))
    {
        return -1;
    }

    const char *p = strstr(json, needle);
    if (p == NULL)
    {
        return -1;
    }
    p += strlen(needle);

    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')
    {
        p++;
    }
    if (*p != ':')
    {
        return -1;
    }
    p++;
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r')
    {
        p++;
    }
    if (*p != '"') /* chỉ hỗ trợ giá trị string theo control_v1 */
    {
        return -1;
    }
    p++;

    size_t i = 0;
    while (*p != '"' && *p != '\0')
    {
        if (i + 1 >= out_size)
        {
            return -1; /* quá dài — từ chối, không tràn buffer */
        }
        out[i++] = *p++;
    }
    if (*p != '"') /* hết payload trước dấu " đóng */
    {
        return -1;
    }
    out[i] = '\0';
    return 0;
}

/* Copy bounded (truncate an toàn) — dùng thay strlcpy để không phụ thuộc
 * tiện ích có thể thiếu trên một số cấu hình newlib. */
static void telemetry_copy_str(char *dst, size_t dst_size, const char *src)
{
    (void)snprintf(dst, dst_size, "%s", src);
}

/* Map lỗi driver → error_code của schema, dạng CHUỖI JSON SẴN QUOTE (literal
 * tĩnh, không cần format lúc build payload). */
static const char *telemetry_error_code_json(hcsr04_error_t err)
{
    switch (err)
    {
    case HCSR04_ERR_TRIG_FAIL:
        return "\"TRIG_FAIL\"";
    case HCSR04_ERR_STUCK_LOW:
        return "\"ECHO_STUCK_LOW\"";
    case HCSR04_ERR_STUCK_HIGH:
        return "\"ECHO_STUCK_HIGH\"";
    case HCSR04_ERR_TIMEOUT:
        return "\"ECHO_TIMEOUT\"";
    default:
        /* Không bao giờ xảy ra với enum hiện tại của hcsr04.h; chọn
         * ECHO_TIMEOUT làm fallback vẫn nằm trong enum của schema. */
        return "\"ECHO_TIMEOUT\"";
    }
}

/*
 * Format epoch UTC (ms) → "YYYY-MM-DDTHH:MM:SS.mmmZ" (không kèm quote;
 * caller tự bọc quote khi nhét vào JSON). Cần out >= 25 byte.
 */
static esp_err_t telemetry_format_utc_ms(int64_t utc_ms, char *out, size_t out_size)
{
    if (utc_ms < 0 || out_size < 25)
    {
        return ESP_ERR_INVALID_ARG;
    }

    const time_t sec = (time_t)(utc_ms / 1000);
    const int ms = (int)(utc_ms % 1000); /* utc_ms >= 0 nên 0..999 */

    struct tm tm_utc;
    if (gmtime_r(&sec, &tm_utc) == NULL)
    {
        return ESP_ERR_INVALID_STATE;
    }

    /* strftime hết chỗ trả 0; 19 ký tự + '\0' trong 32 byte là thoải mái. */
    const size_t n = strftime(out, out_size, "%Y-%m-%dT%H:%M:%S", &tm_utc);
    if (n == 0)
    {
        return ESP_ERR_INVALID_STATE;
    }

    const int w = snprintf(out + n, out_size - n, ".%03dZ", ms);
    if (w < 0 || (size_t)w >= out_size - n)
    {
        return ESP_ERR_INVALID_STATE;
    }
    return ESP_OK;
}

/*
 * Build + publish ack cho một lệnh control lấy từ mailbox. ĐÚNG 9 field theo
 * schemas/ack_v1.schema.json:
 *   schema_version, command, session_id, device_id, boot_id, sequence,
 *   monotonic_us, time_synced, accepted.
 *
 * Quyết định ack (nhất quán: chỉ ack lệnh hợp lệ có session_id):
 *   - session_start/session_stop + có session_id → ack accepted = true.
 *   - lệnh lạ (HRC_CMD_UNKNOWN)                  → log warn, KHÔNG ack:
 *     echo chuỗi lạ vào JSON vừa vi phạm enum command của ack_v1 vừa không
 *     escape an toàn (command_raw chỉ dùng cho log).
 *   - thiếu session_id (command hợp lệ)          → log warn, KHÔNG ack.
 */
static void telemetry_ack_control(const hrc_control_msg_t *msg)
{
    if (msg->command == HRC_CMD_UNKNOWN)
    {
        ESP_LOGW(TAG, "lệnh lạ ngoài control_v1: '%s' — bỏ qua, không ack",
                 msg->command_raw);
        return;
    }
    if (msg->session_id[0] == '\0')
    {
        ESP_LOGW(TAG, "control '%s' thiếu session_id — bỏ qua, không ack",
                 msg->command_raw);
        return;
    }
    const bool accepted = true; /* các nhánh còn lại đều chấp nhận lệnh */

#if CONFIG_HRC_LSTM_INFERENCE
    /* Mỗi session có một cửa sổ thời gian độc lập. Reset trước khi ACK để
     * mẫu đầu tiên sau START không mang lịch sử của session trước; STOP cũng
     * dọn trạng thái trước lượt kế tiếp. LSTM chỉ là advisory, không chặn ACK
     * hoặc telemetry. */
    if (msg->command == HRC_CMD_SESSION_START ||
        msg->command == HRC_CMD_SESSION_STOP)
    {
        lstm_inference_init();
    }
#endif

    /* Sequence của mẫu đã phát gần nhất; nếu chưa có mẫu nào (sequence nội
     * bộ 0) dùng 1 vì schema yêu cầu minimum 1 ⇒ nếu iot_backend mở phiên ngay
     * lúc boot, ranh giới phiên bắt đầu từ mẫu 2 (edge case, xem README). */
    const uint32_t ack_sequence = (s_sequence > 0) ? s_sequence : 1;
    const int64_t now_monotonic_us = esp_timer_get_time();
    const bool time_synced = hrc_time_synced();

    char buf[HRC_JSON_BUF_LEN];
    const int n = snprintf(buf, sizeof(buf),
                           "{\"schema_version\":\"1.0\",\"command\":\"%s\","
                           "\"session_id\":\"%s\",\"device_id\":\"%s\","
                           "\"boot_id\":\"%s\",\"sequence\":%" PRIu32 ","
                           "\"monotonic_us\":%" PRId64 ",\"time_synced\":%s,"
                           "\"accepted\":%s}",
                           msg->command_raw, msg->session_id,
                           CONFIG_HRC_DEVICE_ID, s_boot_id, ack_sequence,
                           now_monotonic_us, time_synced ? "true" : "false",
                           accepted ? "true" : "false");
    if (n < 0 || n >= (int)sizeof(buf))
    {
        ESP_LOGE(TAG, "build ack JSON thất bại (n=%d)", n);
        return;
    }

    if (hrc_mqtt_publish(s_topic_ack, buf, n) != ESP_OK)
    {
        ESP_LOGW(TAG, "publish ack thất bại (command=%s session=%s)",
                 msg->command_raw, msg->session_id);
        return;
    }
    ESP_LOGI(TAG, "ack queued %s session=%s accepted=%d sequence=%" PRIu32,
             msg->command_raw, msg->session_id, accepted ? 1 : 0, ack_sequence);
}

/*
 * Một chu kỳ đo + publish telemetry. ĐÚNG 11 field theo
 * schemas/telemetry_v1.schema.json:
 *   schema_version, device_id, boot_id, sequence, monotonic_us, measured_at,
 *   time_synced, distance_mm, echo_us, valid, error_code.
 */
static void telemetry_sample_and_publish(void)
{
    /* (1) Mốc thời gian TRƯỚC khi đo: utc_ms đọc trước measure_once nên
     * measured_at lệch so với mốc monotonic trong driver một khoảng nhỏ
     * (thiết lập phiên nhận RMT, cỡ vài chục µs) — chấp nhận được với
     * timestamp mức ms (quyết định đã ghi trong README). */
    const bool synced = hrc_time_synced();
    const int64_t utc_ms = hrc_time_utc_ms();

    /* (2) Đo blocking (chậm nhất ~3 × CONFIG_HRC_ECHO_TIMEOUT_MS kể cả
     * grace chờ callback trong driver). */
    const hcsr04_result_t r = hcsr04_measure_once();

    /* (3) Mẫu đầu tiên là sequence 1 (schema minimum 1). */
    const uint32_t sequence = ++s_sequence;
    s_samples = sequence;
    const int64_t monotonic_us = r.start_monotonic_us;

    /* (4) Quy đổi NGUYÊN: quãng đường = v·t/2 →
     *   distance_mm = echo_us[µs] * v[m/s] / 2000
     * Số học int32 đủ: trung gian tối đa 30000 × 400 = 12e6 << 2^31. */
    const int32_t distance_mm =
        (r.error == HCSR04_OK)
            ? (r.echo_us * CONFIG_HRC_SOUND_SPEED_M_S / 2000)
            : 0;

    /* (5) Phân loại theo schema. Các biến *_json là chuỗi JSON thay thế:
     * số (không quote) hoặc null. */
    bool valid = false;
    const char *error_code_json = "null";
    char distance_json[16] = "null";
    char echo_json[16] = "null";

    if (r.error == HCSR04_OK)
    {
        /* Echo đã đo được → echo_us luôn là số trong cả hai nhánh. */
        (void)snprintf(echo_json, sizeof(echo_json), "%" PRId32, r.echo_us);
        if (distance_mm <= HRC_MAX_DISTANCE_MM)
        {
            valid = true;
            error_code_json = "null";
            (void)snprintf(distance_json, sizeof(distance_json), "%" PRId32,
                           distance_mm);
        }
        else
        {
            /* Echo hoàn tất nhưng vật thể ngoài miền biểu diễn (schema max
             * 4500 mm): invalid ECHO_TIMEOUT, KHÔNG clamp — giữ số liệu đo
             * được ở echo_us, distance_mm = null. */
            valid = false;
            error_code_json = "\"ECHO_TIMEOUT\"";
        }
    }
    else
    {
        /* Driver báo lỗi: distance_mm = null; echo_us chỉ giữ khi >= 1
         * (hiện tại driver luôn trả -1 khi lỗi — điều kiện này phòng khi
         * driver thay đổi ngữ nghĩa). */
        valid = false;
        error_code_json = telemetry_error_code_json(r.error);
        if (r.echo_us >= 1)
        {
            (void)snprintf(echo_json, sizeof(echo_json), "%" PRId32, r.echo_us);
        }
    }

    hcsr04_error_t sensor_error = valid ? HCSR04_OK :
        (r.error == HCSR04_OK ? HCSR04_ERR_TIMEOUT : r.error);
    s_sensor_error = (unsigned)sensor_error;
    if (!valid) s_invalid_samples++;
    uint32_t previous_errors = s_sensor_fault.consecutive;
    hrc_fault_action_t sensor_action = hrc_fault_update(&s_sensor_fault, sensor_error, esp_timer_get_time());
    if (sensor_action == HRC_FAULT_REPORT) {
        ESP_LOGW(TAG, "HC-SR04 %s: consecutive=%" PRIu32 " (TRIG=GPIO%d ECHO=GPIO%d); "
                      "invalid telemetry remains UNKNOWN; check wiring/power/Echo divider",
                 telemetry_error_code_json(sensor_error), s_sensor_fault.consecutive,
                 CONFIG_HRC_TRIG_GPIO, CONFIG_HRC_ECHO_GPIO);
    } else if (sensor_action == HRC_FAULT_RECOVERED) {
        ESP_LOGI(TAG, "HC-SR04 recovered after %" PRIu32 " invalid samples", previous_errors);
    }

#if CONFIG_HRC_LSTM_INFERENCE
    /* LSTM is a local advisory path. It never gates or changes telemetry_v1. */
    const float distance_m = (float)distance_mm / 1000.0f;
    (void)lstm_inference_push(sequence, monotonic_us, distance_m, valid);
#endif

#if CONFIG_HRC_DISTANCE_LOG_PERIOD_MS > 0
    int64_t log_us = esp_timer_get_time();
    if (!s_distance_log_us || log_us - s_distance_log_us >=
                             (int64_t)CONFIG_HRC_DISTANCE_LOG_PERIOD_MS * 1000) {
        s_distance_log_us = log_us;
        ESP_LOGI(TAG, "distance_mm=%s echo_us=%s valid=%d error=%s sequence=%" PRIu32,
                 distance_json, echo_json, (int)valid, error_code_json, sequence);
#if CONFIG_HRC_LSTM_INFERENCE
        {
            const lstm_inference_result_t *result = lstm_inference_latest();
            char predicted_ms[8] = "null";
            if (result->has_predicted_violation)
            {
                (void)snprintf(predicted_ms, sizeof(predicted_ms), "%u",
                               (unsigned)result->predicted_violation_ms);
            }
            ESP_LOGI(TAG, "lstm_status=%s sample_count=%u threshold_m=%.6f "
                          "forecast=[%.6f,%.6f,%.6f,%.6f,%.6f] "
                          "predicted_violation_ms=%s early_warning=%d sequence=%" PRIu32,
                     telemetry_lstm_status_name(), (unsigned)result->sample_count,
                     (double)result->threshold_m,
                     (double)result->distance_m[0], (double)result->distance_m[1],
                     (double)result->distance_m[2], (double)result->distance_m[3],
                     (double)result->distance_m[4], predicted_ms,
                     result->early_warning ? 1 : 0, sequence);
        }
#endif
    }
#endif

    /* (6) measured_at: chỉ khi đã sync SNTP. Nếu format bất thường thất bại
     * thì hạ time_synced = false để giữ invariant schema: time_synced = true
     * ⟺ measured_at là chuỗi ISO-8601. KHÔNG thay bằng giờ khác. */
    char measured_at_iso[HRC_MEASURED_AT_LEN];
    char measured_at_quoted[HRC_MEASURED_AT_LEN + 2]; /* ISO string + two JSON quotes */
    const char *measured_at_ptr = "null";
    bool time_synced_out = false;
    if (synced && telemetry_format_utc_ms(utc_ms, measured_at_iso,
                                          sizeof(measured_at_iso)) == ESP_OK)
    {
        /* Bọc quote cho giá trị string trong JSON. */
        (void)snprintf(measured_at_quoted, sizeof(measured_at_quoted), "\"%s\"",
                       measured_at_iso);
        measured_at_ptr = measured_at_quoted;
        time_synced_out = true;
    }

    /* (7) Build payload — đúng 11 field, đúng thứ tự enum của schema,
     * null KHÔNG có quote. */
    char buf[HRC_JSON_BUF_LEN];
    const int n = snprintf(buf, sizeof(buf),
                           "{\"schema_version\":\"1.0\",\"device_id\":\"%s\","
                           "\"boot_id\":\"%s\",\"sequence\":%" PRIu32 ","
                           "\"monotonic_us\":%" PRId64 ",\"measured_at\":%s,"
                           "\"time_synced\":%s,\"distance_mm\":%s,"
                           "\"echo_us\":%s,\"valid\":%s,\"error_code\":%s}",
                           CONFIG_HRC_DEVICE_ID, s_boot_id, sequence,
                           monotonic_us, measured_at_ptr,
                           time_synced_out ? "true" : "false", distance_json,
                           echo_json, valid ? "true" : "false", error_code_json);
    if (n < 0 || n >= (int)sizeof(buf))
    {
        ESP_LOGE(TAG, "build telemetry JSON thất bại (n=%d)", n);
        return;
    }

    /* (8) Zero-wait RAM mailbox. The MQTT worker handles the SDK mutex,
     * retransmission and outbox backpressure without blocking measurement. */
    esp_err_t queued = hrc_mqtt_enqueue(s_topic_telemetry, buf, n);
    uint32_t previous_drops = s_enqueue_fault.consecutive;
    hrc_fault_action_t enqueue_action = hrc_fault_update(&s_enqueue_fault, (uint32_t)queued,
                                                        esp_timer_get_time());
    if (queued != ESP_OK) {
        s_dropped_samples++;
        if (enqueue_action == HRC_FAULT_REPORT) {
            ESP_LOGW(TAG, "telemetry enqueue loss: %s, consecutive=%" PRIu32
                          " total=%u last_sequence=%" PRIu32 " wifi=%d mqtt=%d control=%d pending=%u outbox=%d bytes",
                     esp_err_to_name(queued), s_enqueue_fault.consecutive,
                     (unsigned)s_dropped_samples, sequence, (int)hrc_wifi_connected(),
                     (int)hrc_mqtt_connected(), (int)hrc_mqtt_control_ready(),
                     hrc_mqtt_pending_count(), hrc_mqtt_outbox_size());
        }
        return;
    }
    if (enqueue_action == HRC_FAULT_RECOVERED) {
        ESP_LOGI(TAG, "telemetry enqueue recovered after %" PRIu32 " dropped samples", previous_drops);
    }
    ESP_LOGD(TAG, "sample %" PRIu32 ": %s", sequence, buf);
}

void telemetry_init(void)
{
    /* boot_id: 64 bit ngẫu nhiên mỗi boot → 16 ký tự hex. esp_random() được
     * gọi trong app_main trước khi LSTM khởi động — entropy có thể yếu hơn sau
     * khi Wi-Fi start; mục tiêu là phân biệt các lần boot, không phải khóa
     * mật mã (xem comment trong README về điều chưa kiểm chứng). */
    const uint32_t rnd0 = esp_random();
    const uint32_t rnd1 = esp_random();
    (void)snprintf(s_boot_id, sizeof(s_boot_id), "%08" PRIx32 "%08" PRIx32,
                   rnd0, rnd1);

    s_sequence = 0; /* chưa có mẫu nào; mẫu đầu tiên sẽ là 1 */

#if CONFIG_HRC_LSTM_INFERENCE
    lstm_inference_init();
#endif

    const int n_tel = snprintf(s_topic_telemetry, sizeof(s_topic_telemetry),
                               "hrc/v1/devices/%s/telemetry",
                               CONFIG_HRC_DEVICE_ID);
    const int n_ack = snprintf(s_topic_ack, sizeof(s_topic_ack),
                               "hrc/v1/devices/%s/ack", CONFIG_HRC_DEVICE_ID);
    if (n_tel < 0 || n_tel >= (int)sizeof(s_topic_telemetry) ||
        n_ack < 0 || n_ack >= (int)sizeof(s_topic_ack))
    {
        ESP_LOGE(TAG, "topic bị cắt — CONFIG_HRC_DEVICE_ID quá dài");
    }

    s_control_queue = xQueueCreate(HRC_CONTROL_QUEUE_LEN, sizeof(hrc_control_msg_t));
    if (s_control_queue == NULL)
    {
        ESP_LOGE(TAG, "tạo mailbox control thất bại — lệnh control sẽ bị bỏ");
    }

    ESP_LOGI(TAG, "telemetry ready: boot_id=%s", s_boot_id);
}

void telemetry_handle_control(const char *topic, const char *payload, int len)
{
    (void)topic;    /* wifi_mqtt đã lọc đúng topic control */
    (void)len;      /* payload luôn null-terminated theo hrc_control_cb_t */
    if (payload == NULL)
    {
        return;
    }

    if (s_control_queue == NULL)
    {
        ESP_LOGW(TAG, "mailbox control chưa sẵn sàng — bỏ lệnh");
        return;
    }

    /* Parse tối thiểu ngay tại đây (bounded, không block — an toàn trong
     * task esp-mqtt); quyết định ack để telemetry_task làm sau này. */
    char command_raw[HRC_CMD_RAW_LEN] = "";
    if (telemetry_json_find_string(payload, "command", command_raw,
                                   sizeof(command_raw)) != 0)
    {
        ESP_LOGW(TAG, "control không parse được command — bỏ qua, không ack");
        return;
    }

    char session_id[HRC_SESSION_ID_LEN] = "";
    if (telemetry_json_find_string(payload, "session_id", session_id,
                                   sizeof(session_id)) != 0)
    {
        session_id[0] = '\0'; /* thiếu session_id — task sẽ không ack */
    }

    hrc_control_msg_t msg;
    if (strcmp(command_raw, "session_start") == 0)
    {
        msg.command = HRC_CMD_SESSION_START;
    }
    else if (strcmp(command_raw, "session_stop") == 0)
    {
        msg.command = HRC_CMD_SESSION_STOP;
    }
    else
    {
        msg.command = HRC_CMD_UNKNOWN;
    }
    telemetry_copy_str(msg.command_raw, sizeof(msg.command_raw), command_raw);
    telemetry_copy_str(msg.session_id, sizeof(msg.session_id), session_id);

    if (xQueueSend(s_control_queue, &msg, 0) != pdTRUE)
    {
        ESP_LOGW(TAG, "mailbox control đầy — bỏ lệnh '%s'", command_raw);
    }
}

void telemetry_task(void *arg)
{
    (void)arg;

    TickType_t last_wake = xTaskGetTickCount();
    for (;;)
    {
        if (xTaskDelayUntil(&last_wake, pdMS_TO_TICKS(CONFIG_HRC_SAMPLE_PERIOD_MS)) != pdTRUE) {
            /* Skip missed deadlines instead of bursting to catch up. */
            last_wake = xTaskGetTickCount();
        }

        /* (a) Mailbox control: xả hết hàng đợi hiện có trước khi đo. */
        if (s_control_queue != NULL)
        {
            hrc_control_msg_t msg;
            while (xQueueReceive(s_control_queue, &msg, 0) == pdTRUE)
            {
                telemetry_ack_control(&msg);
            }
        }

        /* (b) Đo + build + publish telemetry. */
        telemetry_sample_and_publish();
    }
}
