#pragma once
#include "idf_stub.h"
typedef struct { uint32_t tx_buffer_size,rx_buffer_size; } usb_serial_jtag_driver_config_t;
bool usb_serial_jtag_is_driver_installed(void);
esp_err_t usb_serial_jtag_driver_install(usb_serial_jtag_driver_config_t *);
int usb_serial_jtag_read_bytes(void *,uint32_t,uint32_t);
