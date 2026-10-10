/*
 * HRC Safety Log — payload telemetry_v1 + session control + app loop
 * (ESP-IDF v5.x) — M4d
 *
 * Module nối driver HC-SR04 (M4b) với lớp mạng (M4c):
 *   - Đo định kỳ theo CONFIG_HRC_SAMPLE_PERIOD_MS, build payload
 *     telemetry_v1 đúng schemas/telemetry_v1.schema.json và publish
 *     lên topic hrc/v1/devices/<device_id>/telemetry (QoS1).
 *   - Nhận lệnh control_v1 qua mailbox (queue), trả ack_v1 lên topic
 *     hrc/v1/devices/<device_id>/ack (QoS1). Ranh giới session iot_backend
 *     lấy từ ack này (schemas/ack_v1.schema.json).
 *
 * Tách ngữ cảnh: callback MQTT (telemetry_handle_control) chỉ đẩy lệnh
 * vào queue nội bộ, KHÔNG publish — mọi việc nặng (parse, build JSON,
 * publish) nằm trong telemetry_task.
 */
#pragma once

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Khởi tạo trạng thái module (gọi MỘT lần từ app_main, trước khi tạo task):
 *   - sinh boot_id 16 ký tự hex từ esp_random() (một lần mỗi boot, dùng làm
 *     khóa mẫu cùng device_id + sequence);
 *   - đặt sequence nội bộ = 0 (chưa có mẫu nào; mẫu đầu tiên sẽ là 1);
 *   - build sẵn 2 topic tĩnh:
 *       telemetry: hrc/v1/devices/<CONFIG_HRC_DEVICE_ID>/telemetry
 *       ack      : hrc/v1/devices/<CONFIG_HRC_DEVICE_ID>/ack
 */
void telemetry_init(void);

/*
 * Callback nhận lệnh control từ broker (khớp hrc_control_cb_t, đăng ký qua
 * hrc_mqtt_on_control). CHỈ parse tối thiểu và copy lệnh vào mailbox nội bộ
 * (FreeRTOS queue depth 4): nếu queue đầy thì bỏ lệnh và log warn.
 * KHÔNG publish trong ngữ cảnh này (đang chạy trong task esp-mqtt).
 *   topic   — không dùng (đã được wifi_mqtt lọc đúng topic control).
 *   payload — null-terminated; copy khi cần dùng sau khi trả.
 *   len     — độ dài payload, KHÔNG tính '\0'.
 */
void telemetry_handle_control(const char *topic, const char *payload, int len);

/*
 * Task đo + publish (chạy mãi, tạo từ app_main bằng xTaskCreate):
 *   - chu kỳ CONFIG_HRC_SAMPLE_PERIOD_MS bằng vTaskDelayUntil (không trôi
 *     chu kỳ do thời gian đo/publish);
 *   - mỗi chu kỳ: xử lý lệnh control trong mailbox (nếu có), rồi đo
 *     hcsr04_measure_once(), build payload telemetry_v1 và publish.
 *   arg — không dùng (truyền NULL).
 */
void telemetry_task(void *arg);

/* Snapshot for serial diagnostics; counters are atomic. */
void telemetry_status(char *out, size_t capacity);

#ifdef __cplusplus
}
#endif
