/*
 * HRC Safety Log — Wi-Fi STA + SNTP + MQTT client (ESP-IDF v5.x) — M4c
 *
 * Public API lớp mạng của firmware (M4d sẽ nối):
 *   - hrc_net_init()        : NVS + esp_netif + event loop + Wi-Fi STA,
 *                             blocking chờ IP (15 s); SNTP tự khởi động
 *                             ngay khi STA có IP (TZ = UTC).
 *   - hrc_time_synced()     : true sau khi SNTP sync thành công >= 1 lần.
 *   - hrc_time_utc_ms()     : epoch UTC (ms) — chỉ có nghĩa khi synced.
 *   - hrc_mqtt_on_control() : đăng ký callback nhận lệnh control.
 *   - hrc_mqtt_start()      : MQTT client tới CONFIG_HRC_MQTT_URI,
 *                             tự subscribe topic control QoS1.
 *   - hrc_mqtt_connected()  : trạng thái phiên MQTT.
 *   - hrc_mqtt_publish()    : publish QoS1, retain=false (blocking — cho
 *                             ack/lệnh hiếm, nhỏ).
 *   - hrc_mqtt_enqueue()    : enqueue publish QoS1 non-blocking
 *                             (retain=false, store=true) — cho telemetry
 *                             định kỳ, không block task đo khi mạng xấu.
 *
 * Tự reconnect: Wi-Fi retry vô hạn khi mất STA; esp-mqtt tự reconnect khi
 * mất TCP/MQTT; SNTP lwIP tự truy vấn lại định kỳ. Module KHÔNG build
 * payload/JSON (M4d) và KHÔNG đụng phần cứng đo (M4b).
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Callback nhận lệnh control từ broker.
 *
 * Được gọi trong ngữ cảnh task nội bộ của esp-mqtt: KHÔNG block, KHÔNG gọi
 * lại các hàm hrc_mqtt_* từ đây (nguy cơ deadlock) — chỉ lưu lệnh/đặt cờ.
 *   topic   — tên topic, null-terminated (chính là topic control).
 *   payload — nội dung lệnh, LUÔN null-terminated.
 *   len     — độ dài payload, KHÔNG tính '\0' cuối.
 */
typedef void (*hrc_control_cb_t)(const char *topic, const char *payload, int len);

/*
 * Khởi tạo toàn bộ lớp mạng (gọi MỘT lần từ app_main, trước hrc_mqtt_start):
 *   1. nvs_flash_init() — tự erase khi partition mới/hết trang rồi init lại.
 *   2. esp_netif_init() + event loop mặc định + default STA netif.
 *   3. esp_wifi_init/set_mode(STA)/set_config(CONFIG_HRC_WIFI_SSID,
 *      CONFIG_HRC_WIFI_PASS)/start().
 *   4. Blocking chờ IP tối đa 15 s; khi có IP, SNTP được khởi động tự động.
 *
 * Trả ESP_OK khi đã có IP (IP_EVENT_STA_GOT_IP); ESP_ERR_TIMEOUT nếu hết
 * 15 s chưa có IP — Wi-Fi vẫn tiếp tục tự reconnect nền và SNTP sẽ tự
 * khởi động tại lần có IP sau. Các lần gọi thêm sau lần đầu KHÔNG tạo lại
 * tài nguyên: trả ESP_OK nếu đang connected, ESP_ERR_INVALID_STATE nếu chưa.
 */
esp_err_t hrc_net_init(void);

/*
 * true khi SNTP đã sync thành công ít nhất một lần kể từ lúc boot
 * (qua callback esp_sntp_set_time_sync_notification_cb). Không tự giảm về
 * false khi mất sync tạm thời.
 */
bool hrc_time_synced(void);

/*
 * Thời điểm hiện tại dạng epoch UTC tính bằng ms.
 * Chỉ có nghĩa khi hrc_time_synced() == true; trước khi sync, giá trị trả
 * về là epoch của clock hệ thống (mặc định 1970-01-01 + uptime).
 */
int64_t hrc_time_utc_ms(void);

/*
 * Đăng ký callback nhận lệnh control trên topic
 * hrc/v1/devices/<CONFIG_HRC_DEVICE_ID>/control (QoS1).
 * Gọi TRƯỚC hrc_mqtt_start(); truyền NULL để bỏ đăng ký.
 */
void hrc_mqtt_on_control(hrc_control_cb_t cb);

/*
 * Tạo và khởi động MQTT client tới CONFIG_HRC_MQTT_URI
 * (esp_mqtt_client_init + register_event + start). Khi MQTT_EVENT_CONNECTED
 * sẽ tự subscribe topic control QoS1; esp-mqtt tự reconnect khi mất kết nối.
 *
 * Nên gọi sau khi hrc_net_init() trả ESP_OK (nếu gọi trước, esp-mqtt vẫn tự
 * retry kết nối broker khi mạng sẵn sàng). Trả ESP_OK khi client đã khởi
 * động; ESP_ERR_INVALID_STATE nếu client tạo thất bại; ESP_FAIL cho lỗi
 * khác. Gọi thêm lần nữa khi đã start là no-op trả ESP_OK.
 */
esp_err_t hrc_mqtt_start(void);

/* true khi phiên MQTT với broker đang hoạt động (true sau
 * MQTT_EVENT_CONNECTED, false lại sau MQTT_EVENT_DISCONNECTED). */
bool hrc_mqtt_connected(void);

/*
 * Publish payload lên topic (QoS1, retain=false) — BLOCKING: có thể chờ
 * network timeout tới ~10 s khi mạng xấu (esp_mqtt_client_publish gửi ngay
 * trong task gọi). Dùng cho message hiếm và nhỏ (ack lệnh control); publish
 * định kỳ tần suất cao (telemetry) nên dùng hrc_mqtt_enqueue().
 *   topic   — null-terminated.
 *   payload — dữ liệu thô (không cần null-terminated).
 *   len     — số byte payload; truyền < 0 để esp-mqtt tự strlen(payload).
 * Trả ESP_OK khi client đã nhận message để gửi (msg_id >= 0, chưa tính ack
 * QoS1; esp-mqtt outbox lưu message nếu chưa connected và gửi khi kết nối
 * được thiết lập), ESP_ERR_INVALID_STATE nếu hrc_mqtt_start() chưa được gọi,
 * ESP_FAIL nếu esp-mqtt từ chối message (lỗi nội bộ).
 */
esp_err_t hrc_mqtt_publish(const char *topic, const char *payload, int len);

/*
 * Enqueue payload lên topic KHÔNG block (QoS1, retain=false, store=true):
 * message được ghi vào outbox nội bộ của esp-mqtt và task esp-mqtt sẽ gửi
 * sau (kể cả khi chưa connected — gửi ngay khi kết nối được thiết lập lại).
 * Dùng cho publish định kỳ tần suất cao (telemetry) để task đo không bị
 * chặn network timeout ~10 s khi mạng xấu.
 *   topic   — null-terminated.
 *   payload — dữ liệu thô (không cần null-terminated).
 *   len     — số byte payload; truyền < 0 để esp-mqtt tự strlen(payload).
 * Trả ESP_OK khi message đã vào outbox (dung lượng outbox giới hạn bằng
 * .outbox.limit đặt trong hrc_mqtt_start()), ESP_ERR_INVALID_STATE nếu
 * hrc_mqtt_start() chưa được gọi, ESP_FAIL nếu esp-mqtt từ chối message
 * (bao gồm outbox đầy — esp_mqtt_client_enqueue trả -1/-2).
 */
esp_err_t hrc_mqtt_enqueue(const char *topic, const char *payload, int len);

#ifdef __cplusplus
}
#endif
