#include "network_setup.h"
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct {
    const hrc_setup_ops_t *ops;
    bool skip_lf;
} input_t;

typedef enum { LINE_OK, LINE_CANCEL, LINE_OVERFLOW, LINE_STOP } line_result_t;

/* Volatile writes keep passwords from surviving in the console's stack buffer. */
static void clear_secret(char *data, size_t length)
{
    volatile char *p = data;
    while (length--) *p++ = 0;
}

static line_result_t read_line(input_t *input, char *out, size_t capacity, bool secret)
{
    size_t used = 0;
    bool overflow = false;
    memset(out, 0, capacity);
    for (;;) {
        int c = input->ops->read_byte(input->ops->ctx);
        if (c == -2) { clear_secret(out, capacity); return LINE_STOP; }
        if (c < 0) continue;
        if (input->skip_lf) {
            input->skip_lf = false;
            if (c == '\n') continue;
        }
        if (c == '\r' || c == '\n') {
            input->skip_lf = c == '\r';
            input->ops->write_text(input->ops->ctx, "\n");
            if (overflow) { clear_secret(out, capacity); return LINE_OVERFLOW; }
            out[used] = '\0';
            return LINE_OK;
        }
        if (c == 3 || c == 27) {
            input->ops->write_text(input->ops->ctx, "\nCancelled.\n");
            clear_secret(out, capacity);
            return LINE_CANCEL;
        }
        if (c == 8 || c == 127) {
            if (used && !overflow) {
                out[--used] = '\0';
                input->ops->write_text(input->ops->ctx, "\b \b");
            }
            continue;
        }
        if (c < 32) continue;
        if (used + 1 >= capacity) { overflow = true; continue; }
        if (overflow) continue;
        out[used++] = (char)c;
        char echo[] = {(char)(secret ? '*' : c), '\0'};
        input->ops->write_text(input->ops->ctx, echo);
    }
}

bool hrc_wifi_credentials_valid(const char *ssid, const char *password)
{
    if (!ssid || !password) return false;
    size_t s = strlen(ssid), p = strlen(password);
    if (s == 0 || s > 32) return false;
    for (size_t i = 0; i < s; i++) if ((unsigned char)ssid[i] < 32 || ssid[i] == 127) return false;
    if (p == 0) return true;
    if (p == 64) {
        for (size_t i = 0; i < p; i++) if (!isxdigit((unsigned char)password[i])) return false;
        return true;
    }
    if (p < 8 || p > 63) return false;
    for (size_t i = 0; i < p; i++) if ((unsigned char)password[i] < 32 || password[i] == 127) return false;
    return true;
}

bool hrc_broker_uri_valid(const char *uri)
{
    if (!uri || strlen(uri) >= HRC_BROKER_URI_MAX || strncmp(uri, "mqtt://", 7)) return false;
    const char *p = uri + 7;
    if (*p == '[') {
        p++;
        const char *host = p;
        bool colon = false;
        while (isxdigit((unsigned char)*p) || *p == ':' || *p == '.') {
            if (*p == ':') colon = true;
            p++;
        }
        if (p == host || !colon || *p++ != ']') return false;
    } else {
        const char *host = p;
        while (isalnum((unsigned char)*p) || *p == '.' || *p == '-') p++;
        if (p == host) return false;
    }
    if (*p == ':') {
        unsigned port = 0;
        p++;
        if (!isdigit((unsigned char)*p)) return false;
        while (isdigit((unsigned char)*p)) {
            port = port * 10 + (unsigned)(*p++ - '0');
            if (port > 65535) return false;
        }
        if (port == 0) return false;
    }
    if (*p == '/') p++;
    return *p == '\0';
}

static bool show_scan(const hrc_setup_ops_t *ops, hrc_setup_ap_t *aps, size_t *count)
{
    *count = 0;
    ops->write_text(ops->ctx, "Scanning Wi-Fi (2.4 GHz)...\n");
    if (ops->scan(ops->ctx, aps, HRC_SETUP_MAX_APS, count) != 0) {
        *count = 0;
        ops->write_text(ops->ctx, "Scan failed; use wifi to retry.\n");
        return false;
    }
    if (*count > HRC_SETUP_MAX_APS) *count = HRC_SETUP_MAX_APS;
    for (size_t i = 0; i < *count; i++) {
        char display[33], line[128];
        memcpy(display, aps[i].ssid, sizeof(display));
        display[32] = '\0';
        for (size_t j = 0; display[j]; j++) {
            if ((unsigned char)display[j] < 32 || display[j] == 127) display[j] = '?';
        }
        snprintf(line, sizeof(line), "  %zu. %-32s %d dBm  ch %u  %s%s\n",
                 i + 1, display, (int)aps[i].rssi, (unsigned)aps[i].channel,
                 aps[i].security, aps[i].supported ? "" : " (unsupported)");
        ops->write_text(ops->ctx, line);
    }
    ops->write_text(ops->ctx, *count ? "Use join N to choose; hidden for a hidden SSID.\n" :
                    "No visible networks. Use wifi to rescan, or hidden.\n");
    return true;
}

void hrc_setup_run(const hrc_setup_ops_t *ops, bool initial_scan)
{
    hrc_setup_ap_t aps[HRC_SETUP_MAX_APS] = {0};
    size_t count = 0;
    input_t input = {.ops = ops};
    const char *help = "Commands: wifi | join N | hidden | broker mqtt://HOST:PORT | status | help\n";
    ops->write_text(ops->ctx, "\nHRC network setup. Passwords are masked; settings are saved in NVS.\n");
    ops->write_text(ops->ctx, help);
    if (initial_scan) show_scan(ops, aps, &count);
    for (;;) {
        char command[160];
        ops->write_text(ops->ctx, "hrc> ");
        line_result_t result = read_line(&input, command, sizeof(command), false);
        if (result == LINE_STOP) return;
        if (result == LINE_OVERFLOW) { ops->write_text(ops->ctx, "Input too long; rejected.\n"); continue; }
        if (result != LINE_OK || !command[0]) continue;
        if (!strcmp(command, "help")) ops->write_text(ops->ctx, help);
        else if (!strcmp(command, "status")) ops->status(ops->ctx);
        else if (!strcmp(command, "wifi") || !strcmp(command, "wifi scan")) show_scan(ops, aps, &count);
        else if (!strncmp(command, "broker ", 7)) {
            const char *uri = command + 7;
            if (!hrc_broker_uri_valid(uri)) ops->write_text(ops->ctx, "Invalid broker. Use mqtt://HOST:PORT (no credentials/path).\n");
            else if (ops->set_broker(ops->ctx, uri) != 0) ops->write_text(ops->ctx, "Broker update failed; check status.\n");
            else ops->write_text(ops->ctx, "Broker updated and saved.\n");
        } else if (!strncmp(command, "join ", 5) || !strcmp(command, "hidden")) {
            char ssid[33], password[65] = {0};
            bool secure = true;
            if (!strcmp(command, "hidden")) {
                ops->write_text(ops->ctx, "SSID: ");
                result = read_line(&input, ssid, sizeof(ssid), false);
                if (result == LINE_STOP) return;
                if (result != LINE_OK) { ops->write_text(ops->ctx, "SSID rejected.\n"); continue; }
            } else {
                char *end;
                unsigned long selected = strtoul(command + 5, &end, 10);
                if (*end || selected == 0 || selected > count) {
                    ops->write_text(ops->ctx, "Invalid index. Run wifi, then join N.\n"); continue;
                }
                const hrc_setup_ap_t *ap = &aps[selected - 1];
                if (!ap->supported) { ops->write_text(ops->ctx, "Network requires unsupported authentication.\n"); continue; }
                memcpy(ssid, ap->ssid, sizeof(ssid));
                ssid[32] = '\0';
                secure = ap->needs_password;
            }
            if (secure) {
                ops->write_text(ops->ctx, "Password (masked, empty for open hidden network): ");
                result = read_line(&input, password, sizeof(password), true);
                if (result == LINE_STOP) return;
                if (result != LINE_OK || (!password[0] && strcmp(command, "hidden"))) {
                    clear_secret(password, sizeof(password));
                    ops->write_text(ops->ctx, "Password rejected; run join again.\n"); continue;
                }
            }
            if (!hrc_wifi_credentials_valid(ssid, password)) {
                clear_secret(password, sizeof(password));
                ops->write_text(ops->ctx, "Invalid SSID/password length. WPA: 8..63 characters or 64 hex.\n"); continue;
            }
            ops->write_text(ops->ctx, "Connecting (up to 15s)...\n");
            bool saved = false;
            int joined = ops->join(ops->ctx, ssid, password, &saved);
            clear_secret(password, sizeof(password));
            if (joined != 0) ops->write_text(ops->ctx, "Connection failed. Previous saved network kept; run wifi to retry.\n");
            else ops->write_text(ops->ctx, saved ? "Connected; saved for next boot.\n" : "Connected, but not saved. Check NVS logs.\n");
        } else ops->write_text(ops->ctx, help);
    }
}
