# Component "main" — HRC Safety Log firmware (ESP32, ESP-IDF v5.5 / v6.0)
#
# app_main starts network/serial setup before the sensor and sampling task.
# network_console supports UART and USB Serial/JTAG (primary or secondary).
#   app_main.c   — init, serial provisioning and telemetry task.
#   hcsr04.c     — driver HC-SR04 bằng RMT RX, 1 tick = 1 µs.
#   wifi_mqtt.c  — Wi-Fi STA + SNTP + MQTT client (QoS1, tự reconnect).
#   telemetry.c  — payload telemetry_v1 (11 field), session control/ack
#                  (mailbox queue depth 4), app loop định kỳ
#                  CONFIG_HRC_SAMPLE_PERIOD_MS.
#
# IDF 6.0 no longer exports the RMT/GPIO drivers through "driver".
# MQTT is bundled in IDF 5.5; IDF 6.0 gets espressif/mqtt through the
# component manifest, which adds its dependency automatically.
set(hrc_private_requires esp_wifi esp_event esp_netif nvs_flash esp_timer
                         esp_driver_gpio esp_driver_rmt esp_driver_uart esp_driver_usb_serial_jtag lwip esp_hw_support)
if(IDF_VERSION_MAJOR LESS 6)
    list(APPEND hrc_private_requires mqtt)
endif()

idf_component_register(SRCS "lstm_inference.c" "network_console.c" "network_setup.c" "diagnostics.c" "app_main.c" "hcsr04.c" "wifi_mqtt.c" "telemetry.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES ${hrc_private_requires})
