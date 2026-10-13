#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define HRC_SETUP_MAX_APS 20
#define HRC_BROKER_URI_MAX 128

typedef struct {
    char ssid[33];
    int8_t rssi;
    uint8_t channel;
    const char *security;
    bool supported;
    bool needs_password;
} hrc_setup_ap_t;

/* read_byte returns a byte, -1 for no input yet, or -2 to stop the console. */
typedef struct {
    void *ctx;
    int (*read_byte)(void *ctx);
    void (*write_text)(void *ctx, const char *text);
    int (*scan)(void *ctx, hrc_setup_ap_t *aps, size_t capacity, size_t *count);
    int (*join)(void *ctx, const char *ssid, const char *password, bool *saved);
    int (*set_broker)(void *ctx, const char *uri);
    void (*status)(void *ctx);
} hrc_setup_ops_t;

bool hrc_broker_uri_valid(const char *uri);
bool hrc_wifi_credentials_valid(const char *ssid, const char *password);
void hrc_setup_run(const hrc_setup_ops_t *ops, bool initial_scan);
