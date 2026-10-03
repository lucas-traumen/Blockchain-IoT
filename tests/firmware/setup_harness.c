#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "network_setup.h"
#include "diagnostics.h"

typedef struct {
    const char *input;
    size_t offset;
    char output[8192];
    unsigned joins, scans, brokers;
    char ssid[33], password[65], broker[128];
    bool saved;
} fixture_t;

static int read_byte(void *ctx) {
    fixture_t *f = ctx;
    unsigned char c = (unsigned char)f->input[f->offset];
    if (!c) return -2;
    f->offset++;
    return c;
}
static void write_text(void *ctx, const char *text) {
    fixture_t *f = ctx;
    assert(strlen(f->output) + strlen(text) < sizeof(f->output));
    strcat(f->output, text);
}
static int scan(void *ctx, hrc_setup_ap_t *aps, size_t capacity, size_t *count) {
    fixture_t *f = ctx;
    f->scans++;
    assert(capacity >= 3);
    aps[0] = (hrc_setup_ap_t){.ssid="Lab_AP", .rssi=-42, .channel=6,
        .security="WPA2", .supported=true, .needs_password=true};
    aps[1] = (hrc_setup_ap_t){.ssid="Guest", .rssi=-60, .channel=1,
        .security="OPEN", .supported=true};
    aps[2] = (hrc_setup_ap_t){.ssid="12345678901234567890123456789012",
        .rssi=-65, .channel=11, .security="WPA2", .supported=true,
        .needs_password=true};
    *count = 3;
    return 0;
}
static int join(void *ctx, const char *ssid, const char *password, bool *saved) {
    fixture_t *f = ctx;
    f->joins++;
    strcpy(f->ssid, ssid);
    strcpy(f->password, password);
    *saved = f->saved;
    return 0;
}
static int broker(void *ctx, const char *uri) {
    fixture_t *f = ctx;
    f->brokers++;
    strcpy(f->broker, uri);
    return 0;
}
static void status(void *ctx) { write_text(ctx, "status: OK\n"); }

int main(int argc, char **argv) {
    assert(argc == 2);
    const char *which = argv[1];
    fixture_t f = {.saved=true};
    if (!strcmp(which, "join")) f.input="join 1\nmock-password\nstatus\n";
    else if (!strcmp(which, "crlf")) f.input="join 1\r\nmock-password\r\n";
    else if (!strcmp(which, "open")) f.input="join 2\n";
    else if (!strcmp(which, "invalid_index")) f.input="join 0\njoin 4\njoin 1x\n";
    else if (!strcmp(which, "overflow")) f.input="join 1\nAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n";
    else if (!strcmp(which, "cancel")) f.input="join 1\nmock\003\n";
    else if (!strcmp(which, "hidden")) f.input="hidden\nHidden_AP\nmock-password\n";
    else if (!strcmp(which, "full_ssid")) f.input="join 3\nmock-password\n";
    else if (!strcmp(which, "invalid_broker")) f.input="broker http://localhost\nbroker mqtt://\nbroker mqtt://host:70000\nbroker mqtt://user:pass@host\nbroker mqtt://host:1883/path\n";
    else if (!strcmp(which, "broker")) f.input="broker mqtt://192.168.10.5:1883\n";
    else if (!strcmp(which, "unsaved")) { f.saved=false; f.input="join 1\nmock-password\n"; }
    else {
        assert(!strcmp(which, "faults"));
        hrc_fault_state_t state = {0};
        assert(hrc_fault_update(&state, 1, 0) == HRC_FAULT_REPORT);
        for (int i=1; i<50; i++) assert(hrc_fault_update(&state, 1, i*100000) == HRC_FAULT_QUIET);
        assert(state.consecutive == 50);
        assert(hrc_fault_update(&state, 1, 5000000) == HRC_FAULT_REPORT);
        assert(hrc_fault_update(&state, 2, 5100000) == HRC_FAULT_REPORT);
        assert(hrc_fault_update(&state, 0, 5200000) == HRC_FAULT_RECOVERED);
        assert(hrc_fault_update(&state, 0, 5300000) == HRC_FAULT_QUIET);
        assert(hrc_fault_update(&state, 1, 5400000) == HRC_FAULT_REPORT);
        return 0;
    }
    hrc_setup_ops_t ops = {.ctx=&f, .read_byte=read_byte, .write_text=write_text,
        .scan=scan, .join=join, .set_broker=broker, .status=status};
    hrc_setup_run(&ops, true);
    assert(f.scans == 1);
    assert(strstr(f.output, "Lab_AP"));
    assert(!strstr(f.output, "mock-password"));
    if (!strcmp(which, "invalid_index") || !strcmp(which, "overflow") ||
        !strcmp(which, "cancel") || !strcmp(which, "invalid_broker")) assert(f.joins == 0);
    else if (!strcmp(which, "broker")) {
        assert(f.brokers == 1);
        assert(!strcmp(f.broker, "mqtt://192.168.10.5:1883"));
    } else {
        assert(f.joins == 1);
        if (!strcmp(which, "open")) assert(!strcmp(f.password, ""));
        else assert(!strcmp(f.password, "mock-password"));
        if (!strcmp(which, "full_ssid")) assert(strlen(f.ssid) == 32);
        if (!strcmp(which, "hidden")) assert(!strcmp(f.ssid, "Hidden_AP"));
        if (!strcmp(which, "unsaved")) assert(strstr(f.output, "not saved"));
    }
    if (!strcmp(which, "invalid_broker")) assert(f.brokers == 0);
    return 0;
}
