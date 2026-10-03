#pragma once
#define ESP_LINE_ENDINGS_CRLF 1
void uart_vfs_dev_port_set_tx_line_endings(int,int);
void uart_vfs_dev_use_driver(int);
