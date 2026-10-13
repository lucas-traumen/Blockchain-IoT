#include "network_console.h"
#include "network_setup.h"
#include "wifi_mqtt.h"
#include "telemetry.h"
#include "sdkconfig.h"
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"

#if CONFIG_HRC_SERIAL_SETUP && (CONFIG_ESP_CONSOLE_UART || CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG)
#include "soc/soc_caps.h"
#if CONFIG_ESP_CONSOLE_UART
#include "driver/uart.h"
#include "driver/uart_vfs.h"
#endif
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG_ENABLED
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#endif

static bool s_initial_scan;
static TaskHandle_t s_console_task;
static int console_read(void *ctx)
{
    (void)ctx;
    uint8_t byte;
#if CONFIG_ESP_CONSOLE_UART
    if (uart_read_bytes(CONFIG_ESP_CONSOLE_UART_NUM, &byte, 1, 0) == 1) return byte;
#endif
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG_ENABLED
    if (usb_serial_jtag_read_bytes(&byte, 1, 0) == 1) return byte;
#endif
    vTaskDelay(pdMS_TO_TICKS(10));
    return -1;
}
static void console_write(void *ctx, const char *text)
{
    (void)ctx;
    fputs(text, stdout);
    fflush(stdout);
}
static int console_scan(void *ctx, hrc_setup_ap_t *aps, size_t capacity, size_t *count)
{
    (void)ctx;
    return hrc_wifi_scan(aps, capacity, count);
}
static int console_join(void *ctx, const char *ssid, const char *password, bool *saved)
{
    (void)ctx;
    return hrc_wifi_connect(ssid, password, saved);
}
static int console_broker(void *ctx, const char *uri)
{
    (void)ctx;
    return hrc_mqtt_set_broker(uri);
}
static void console_status(void *ctx)
{
    char status[384];
    hrc_network_status(status, sizeof(status));
    console_write(ctx, status);
    telemetry_status(status, sizeof(status));
    console_write(ctx, status);
}
static void console_task(void *arg)
{
    (void)arg;
    const hrc_setup_ops_t ops = {.read_byte=console_read, .write_text=console_write,
        .scan=console_scan, .join=console_join, .set_broker=console_broker, .status=console_status};
    hrc_setup_run(&ops, s_initial_scan);
    s_console_task = NULL;
    vTaskDelete(NULL);
}
#endif

esp_err_t hrc_network_console_start(bool initial_scan)
{
#if CONFIG_HRC_SERIAL_SETUP && (CONFIG_ESP_CONSOLE_UART || CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG)
    if (s_console_task) return ESP_OK;
    fflush(stdout);
    esp_err_t err;
#if CONFIG_ESP_CONSOLE_UART
    if (!uart_is_driver_installed(CONFIG_ESP_CONSOLE_UART_NUM)) {
        err = uart_driver_install(CONFIG_ESP_CONSOLE_UART_NUM, 512, 0, 0, NULL, 0);
        if (err != ESP_OK) return err;
    }
    const uart_config_t config = {
        .baud_rate=CONFIG_ESP_CONSOLE_UART_BAUDRATE, .data_bits=UART_DATA_8_BITS,
        .parity=UART_PARITY_DISABLE, .stop_bits=UART_STOP_BITS_1,
#if SOC_UART_SUPPORT_XTAL_CLK
        .source_clk=UART_SCLK_XTAL,
#else
        .source_clk=UART_SCLK_DEFAULT,
#endif
    };
    err = uart_param_config(CONFIG_ESP_CONSOLE_UART_NUM, &config);
    if (err != ESP_OK) return err;
    uart_vfs_dev_port_set_tx_line_endings(CONFIG_ESP_CONSOLE_UART_NUM, ESP_LINE_ENDINGS_CRLF);
    uart_vfs_dev_use_driver(CONFIG_ESP_CONSOLE_UART_NUM);
#endif
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG_ENABLED
    if (!usb_serial_jtag_is_driver_installed()) {
        usb_serial_jtag_driver_config_t usb = {.tx_buffer_size=1024, .rx_buffer_size=512};
        err = usb_serial_jtag_driver_install(&usb);
        if (err != ESP_OK) return err;
    }
    usb_serial_jtag_vfs_set_tx_line_endings(ESP_LINE_ENDINGS_CRLF);
    usb_serial_jtag_vfs_use_driver();
#endif
    s_initial_scan = initial_scan;
    return xTaskCreate(console_task, "network_setup", 8192, NULL, 4, &s_console_task) == pdPASS
        ? ESP_OK : ESP_ERR_NO_MEM;
#else
    (void)initial_scan;
    ESP_LOGW("hrc_console", "Serial setup disabled; use menuconfig or enable UART or USB Serial/JTAG console.");
    return ESP_ERR_NOT_SUPPORTED;
#endif
}
