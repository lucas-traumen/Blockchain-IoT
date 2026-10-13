/* HRC firmware (ESP-IDF 5.5 / 6.0).
 * Boot: telemetry state -> Wi-Fi list/SNTP -> deferred MQTT -> sensor -> sampling.
 * Legacy mode additionally enables serial setup before the sensor.
 */

#include <stdio.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_log.h"

#include "hcsr04.h"
#include "telemetry.h"
#include "wifi_mqtt.h"
#include "network_console.h"

#ifndef CONFIG_HRC_DISTANCE_LOG_PERIOD_MS
#define CONFIG_HRC_DISTANCE_LOG_PERIOD_MS 1000
#endif

static const char *TAG = "hrc_main";

void app_main(void)
{
    /* In cấu hình đọc từ Kconfig (main/Kconfig.projbuild).
     * Default khớp configs/sensor.yaml. */
    ESP_LOGI(TAG, "HRC Safety Log firmware — Wi-Fi scan/list");

    ESP_LOGI(TAG, "device_id           : %s", CONFIG_HRC_DEVICE_ID);
    ESP_LOGI(TAG, "sample period       : %d ms", CONFIG_HRC_SAMPLE_PERIOD_MS);
    ESP_LOGI(TAG, "hcsr04 trig GPIO    : %d", CONFIG_HRC_TRIG_GPIO);
    ESP_LOGI(TAG, "hcsr04 echo GPIO    : %d", CONFIG_HRC_ECHO_GPIO);
    ESP_LOGI(TAG, "hcsr04 echo timeout : %d ms", CONFIG_HRC_ECHO_TIMEOUT_MS);
    ESP_LOGI(TAG, "sound speed         : %d m/s", CONFIG_HRC_SOUND_SPEED_M_S);

    /* 1. Trạng thái telemetry: boot_id (16 hex mỗi boot), sequence = 0,
     *    topic telemetry/ack tĩnh, mailbox control (queue depth 4). */
    telemetry_init();

    hrc_mqtt_on_control(telemetry_handle_control);
    esp_err_t net = hrc_net_init();
    if (net != ESP_OK) {
        ESP_LOGW(TAG, "network not ready: %s; %s", esp_err_to_name(net),
                 hrc_wifi_uses_list() ? "check main/network_config.local.h" : "use serial 'wifi' / 'join N'");
    }
    if (hrc_mqtt_start() != ESP_OK) {
        ESP_LOGW(TAG, "MQTT not started; check broker in %s",
                 hrc_wifi_uses_list() ? "network_config.local.h" : "serial 'status' / 'broker'");
    }
    if (!hrc_wifi_uses_list()) {
        esp_err_t console = hrc_network_console_start(!hrc_wifi_connected());
        if (console != ESP_OK) ESP_LOGW(TAG, "serial setup: %s", esp_err_to_name(console));
    }

    /* Network setup remains usable even if GPIO/RMT initialization fails. */
    if (hcsr04_init() != ESP_OK) {
        ESP_LOGE(TAG, "sensor initialization failed; check TRIG/ECHO GPIO; no telemetry task");
        return;
    }

    /* Task đo + publish định kỳ (stack 4096, ưu tiên 5). */
    if (xTaskCreate(telemetry_task, "telemetry", 4096, NULL, 5, NULL) != pdPASS)
    {
        ESP_LOGE(TAG, "tạo task telemetry thất bại");
    }
    else
    {
        ESP_LOGI(TAG, "telemetry task started; distance log interval=%d ms",
                 CONFIG_HRC_DISTANCE_LOG_PERIOD_MS);
    }

    /* app_main kết thúc — task app của ESP-IDF tự kết thúc bình thường. */
}
