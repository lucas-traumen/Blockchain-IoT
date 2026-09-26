/*
 * HRC Safety Log — ESP32 firmware (ESP-IDF v5.5)
 *
 * Firmware hoàn chỉnh sau M4d (4/4 giai đoạn):
 *   M4a — khung project (CMake, Kconfig, sdkconfig.defaults, app_main).
 *   M4b — driver HC-SR04 (hcsr04.c/.h, đo echo bằng RMT RX).
 *   M4c — Wi-Fi STA + SNTP + MQTT client (wifi_mqtt.c/.h).
 *   M4d — payload telemetry_v1 + session control/ack + app loop
 *         (telemetry.c/.h).
 *
 * Luồng khởi động: telemetry_init → hcsr04_init → đăng ký callback control →
 * hrc_net_init → hrc_mqtt_start → tạo task telemetry (chu kỳ
 * CONFIG_HRC_SAMPLE_PERIOD_MS: xả mailbox control, đo, publish).
 */

#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_log.h"

#include "hcsr04.h"
#include "telemetry.h"
#include "wifi_mqtt.h"

static const char *TAG = "hrc_main";

void app_main(void)
{
    /* In cấu hình đọc từ Kconfig (main/Kconfig.projbuild).
     * Default khớp configs/sensor.yaml. */
    ESP_LOGI(TAG, "HRC Safety Log firmware — hoàn chỉnh (M4a..M4d)");

    ESP_LOGI(TAG, "device_id           : %s", CONFIG_HRC_DEVICE_ID);
    ESP_LOGI(TAG, "wifi ssid           : %s", CONFIG_HRC_WIFI_SSID);
    ESP_LOGI(TAG, "mqtt uri            : %s", CONFIG_HRC_MQTT_URI);
    ESP_LOGI(TAG, "sample period       : %d ms", CONFIG_HRC_SAMPLE_PERIOD_MS);
    ESP_LOGI(TAG, "hcsr04 trig GPIO    : %d", CONFIG_HRC_TRIG_GPIO);
    ESP_LOGI(TAG, "hcsr04 echo GPIO    : %d", CONFIG_HRC_ECHO_GPIO);
    ESP_LOGI(TAG, "hcsr04 echo timeout : %d ms", CONFIG_HRC_ECHO_TIMEOUT_MS);
    ESP_LOGI(TAG, "sound speed         : %d m/s", CONFIG_HRC_SOUND_SPEED_M_S);

    /* 1. Trạng thái telemetry: boot_id (16 hex mỗi boot), sequence = 0,
     *    topic telemetry/ack tĩnh, mailbox control (queue depth 4). */
    telemetry_init();

    /* 2. Cảm biến: thiết bị vô dụng khi không có cảm biến — dừng tại đây. */
    if (hcsr04_init() != ESP_OK)
    {
        ESP_LOGE(TAG, "hcsr04_init thất bại — dừng firmware (không có cảm biến)");
        vTaskDelay(portMAX_DELAY);
    }

    /* 3. Đăng ký callback control TRƯỚC hrc_mqtt_start() (yêu cầu của
     *    wifi_mqtt): callback chỉ đẩy lệnh vào mailbox, không publish. */
    hrc_mqtt_on_control(telemetry_handle_control);

    /* 4. Wi-Fi + SNTP: fail chỉ warn rồi TIẾP TỤC — Wi-Fi tự reconnect nền,
     *    SNTP tự start khi có IP; telemetry vẫn đo và publish với
     *    measured_at = null, time_synced = false cho đến khi sync. */
    if (hrc_net_init() != ESP_OK)
    {
        ESP_LOGW(TAG, "hrc_net_init chưa có IP trong 15 s — tiếp tục "
                      "(Wi-Fi tự reconnect, SNTP tự start khi có IP)");
    }

    /* 5. MQTT: fail chỉ warn — esp-mqtt tự retry kết nối broker. */
    if (hrc_mqtt_start() != ESP_OK)
    {
        ESP_LOGW(TAG, "hrc_mqtt_start thất bại — esp-mqtt sẽ tự retry");
    }

    /* 6. Task đo + publish định kỳ (stack 4096, ưu tiên 5). */
    if (xTaskCreate(telemetry_task, "telemetry", 4096, NULL, 5, NULL) != pdPASS)
    {
        ESP_LOGE(TAG, "tạo task telemetry thất bại");
    }

    /* 7. app_main kết thúc — task app của ESP-IDF tự kết thúc bình thường. */
}
