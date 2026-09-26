# Component "main" — HRC Safety Log firmware (ESP32, ESP-IDF v5.x)
#
# M4d (xong — firmware hoàn chỉnh 4/4 giai đoạn):
#   app_main.c   — khởi động: telemetry_init, hcsr04_init, đăng ký callback
#                  control, hrc_net_init, hrc_mqtt_start, tạo task telemetry.
#   hcsr04.c     — driver HC-SR04 bằng RMT RX, 1 tick = 1 µs.
#   wifi_mqtt.c  — Wi-Fi STA + SNTP + MQTT client (QoS1, tự reconnect).
#   telemetry.c  — payload telemetry_v1 (11 field), session control/ack
#                  (mailbox queue depth 4), app loop định kỳ
#                  CONFIG_HRC_SAMPLE_PERIOD_MS.
#
# PRIV_REQUIRES khai báo sẵn các IDF component mà các module trên dùng;
# không cần sửa thêm phần REQUIRES ở các giai đoạn sau.

idf_component_register(SRCS "app_main.c" "hcsr04.c" "wifi_mqtt.c" "telemetry.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_wifi esp_event esp_netif nvs_flash mqtt esp_timer driver)
