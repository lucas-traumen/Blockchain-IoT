#include "idf_stub.h"
#include "wifi_mqtt.h"
#include "network_console.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/uart.h"
#include "driver/uart_vfs.h"
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#include <assert.h>
#include <string.h>

static bool usb_input, fail_install;
static unsigned offset, task_starts, status_calls;
static const char bytes[]="status\r\n";
const char *esp_err_to_name(esp_err_t err) { (void)err; return "stub-error"; }
void stub_log(const char *format, ...) { (void)format; }
void vTaskDelay(unsigned ticks) { assert(ticks==10); }
void vTaskDelete(TaskHandle_t task) { assert(task==NULL); }
int xTaskCreate(void (*fn)(void *),const char *name,unsigned stack,void *arg,unsigned priority,TaskHandle_t *out) {
    (void)name; (void)priority; assert(stack>=8192); *out=(void *)1; task_starts++; fn(arg); return pdPASS;
}
bool uart_is_driver_installed(int uart) { (void)uart; return false; }
esp_err_t uart_driver_install(int uart,unsigned rx,unsigned tx,int count,void *queue,unsigned flags) {
    (void)uart;(void)tx;(void)count;(void)queue;(void)flags;assert(rx>=256);return fail_install ? ESP_FAIL : ESP_OK;
}
esp_err_t uart_param_config(int uart,const uart_config_t *cfg) { (void)uart;assert(cfg->baud_rate==115200);return ESP_OK; }
void uart_vfs_dev_port_set_tx_line_endings(int uart,int endings) { (void)uart;assert(endings==ESP_LINE_ENDINGS_CRLF); }
void uart_vfs_dev_use_driver(int uart) { (void)uart; }
int uart_read_bytes(int uart,void *out,unsigned count,unsigned ticks) {
    (void)uart; assert(count==1 && ticks==0);
    if (usb_input || offset>=sizeof(bytes)-1) return 0;
    *(char *)out=bytes[offset++];return 1;
}
bool usb_serial_jtag_is_driver_installed(void) { return false; }
esp_err_t usb_serial_jtag_driver_install(usb_serial_jtag_driver_config_t *cfg) {
    assert(cfg->rx_buffer_size>=256 && cfg->tx_buffer_size>0); return fail_install ? ESP_FAIL : ESP_OK;
}
int usb_serial_jtag_read_bytes(void *out,uint32_t count,uint32_t ticks) {
    assert(count==1 && ticks==0);
    if (!usb_input || offset>=sizeof(bytes)-1) return 0;
    *(char *)out=bytes[offset++];return 1;
}
void usb_serial_jtag_vfs_set_tx_line_endings(int endings) { assert(endings==ESP_LINE_ENDINGS_CRLF); }
void usb_serial_jtag_vfs_use_driver(void) {}
esp_err_t hrc_wifi_scan(hrc_setup_ap_t *aps,size_t capacity,size_t *count) { (void)aps;(void)capacity;*count=0;return ESP_OK; }
esp_err_t hrc_wifi_connect(const char *ssid,const char *password,bool *saved) { (void)ssid;(void)password;*saved=true;return ESP_OK; }
esp_err_t hrc_mqtt_set_broker(const char *uri) { (void)uri;return ESP_OK; }
void hrc_network_status(char *out,size_t capacity) { status_calls++;snprintf(out,capacity,"wifi=connected\n"); }
void telemetry_status(char *out,size_t capacity) { snprintf(out,capacity,"sensor=ready\n"); }
void hrc_setup_run(const hrc_setup_ops_t *ops,bool initial_scan) {
    assert(initial_scan);
    for (size_t i=0;i<sizeof(bytes)-1;i++) assert(ops->read_byte(ops->ctx)==bytes[i]);
    assert(ops->read_byte(ops->ctx)==-1);
    ops->status(ops->ctx);
}
int main(int argc,char **argv) {
    assert(argc==2);
    usb_input=!strcmp(argv[1],"usb");fail_install=!strcmp(argv[1],"failure");
    esp_err_t err=hrc_network_console_start(true);
    assert(err==(fail_install ? ESP_FAIL : ESP_OK));
    assert(task_starts==(fail_install ? 0u : 1u));
    assert(status_calls==(fail_install ? 0u : 1u));
    return 0;
}
