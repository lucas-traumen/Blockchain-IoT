#pragma once
#include "idf_stub.h"
#define UART_DATA_8_BITS 8
#define UART_PARITY_DISABLE 0
#define UART_STOP_BITS_1 1
#define UART_SCLK_DEFAULT 0
#define UART_SCLK_XTAL 1
typedef struct { int baud_rate,data_bits,parity,stop_bits,source_clk; } uart_config_t;
bool uart_is_driver_installed(int);
esp_err_t uart_driver_install(int,unsigned,unsigned,int,void *,unsigned);
esp_err_t uart_param_config(int,const uart_config_t *);
int uart_read_bytes(int,void *,unsigned,unsigned);
