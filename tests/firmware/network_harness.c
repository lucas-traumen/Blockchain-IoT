#include "idf_stub.h"
#include "wifi_mqtt.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <assert.h>
#include <stdarg.h>
#include <setjmp.h>
#include <stdlib.h>
#include <string.h>

const char stub_wifi_event[]="wifi", stub_ip_event[]="ip";
static event_callback_t wifi_callback, ip_callback;
static esp_timer_handle_t timer;
static wifi_config_t wifi;
static bool radio_started, has_ip;
static unsigned connects, mqtt_created, scans, commits, nvs_reads, task_starts;
static unsigned subscribe_failures, sdk_enqueues;
static bool measurement_context;
static int64_t fake_time_us=1000000;
static void (*mqtt_sender)(void *);
static void *mqtt_sender_arg;
static jmp_buf sender_exit;
static unsigned sender_rounds, subscribes, forced_disconnects;
static int reject_subscribe, reject_enqueue;
static bool defer_suback;
static char first_transfer_topic[96], last_transfer_topic[96], last_transfer_payload[384];
static esp_mqtt_client_handle_t last_client;
static const char *scenario;
static char attempted[128][33];
static wifi_ap_record_t scan_records[40];
static unsigned scan_count, scan_offset;
static unsigned char saved_wifi[128], saved_broker[128];
static size_t saved_wifi_size, saved_broker_size;
struct mqtt_stub { event_callback_t callback; char uri[128]; int bytes, limit; };

const char *esp_err_to_name(esp_err_t err) { (void)err; return "stub-error"; }
void stub_log(const char *format, ...) {
    char message[512];
    va_list args;va_start(args,format);vsnprintf(message,sizeof(message),format,args);va_end(args);
    assert(!strstr(message,"mock-password") && !strstr(message,"strong-password") && !strstr(message,"wrong-password"));
}
int64_t esp_timer_get_time(void) { return fake_time_us; }
void vTaskDelay(unsigned ticks) {
    if (sender_rounds) {
        fake_time_us+=(int64_t)ticks*1000;
        if (!--sender_rounds) longjmp(sender_exit,1);
    }
}
static void pump_sender(unsigned rounds) {
    assert(mqtt_sender && rounds);
    sender_rounds=rounds;
    if (!setjmp(sender_exit)) mqtt_sender(mqtt_sender_arg);
}
struct queue_stub { unsigned capacity,size,head,count; unsigned char *data; };
QueueHandle_t xQueueCreate(unsigned count,unsigned size) {
    QueueHandle_t q=calloc(1,sizeof(*q));q->capacity=count;q->size=size;q->data=calloc(count,size);return q;
}
int xQueueSend(QueueHandle_t q,const void *item,unsigned wait) {
    assert(wait==0);
    if (q->count==q->capacity) return pdFALSE;
    memcpy(q->data+((q->head+q->count)%q->capacity)*q->size,item,q->size);q->count++;return pdTRUE;
}
int xQueuePeek(QueueHandle_t q,void *item,unsigned wait) {
    assert(wait==0);if (!q->count) return pdFALSE;
    memcpy(item,q->data+q->head*q->size,q->size);return pdTRUE;
}
int xQueueReceive(QueueHandle_t q,void *item,unsigned wait) {
    if (!xQueuePeek(q,item,wait)) return pdFALSE;
    q->head=(q->head+1)%q->capacity;q->count--;return pdTRUE;
}
unsigned uxQueueMessagesWaiting(QueueHandle_t q) { return q->count; }
unsigned uxQueueSpacesAvailable(QueueHandle_t q) { return q->capacity-q->count; }
void vTaskDelete(TaskHandle_t task) { assert(task==NULL); }
int xTaskCreate(void (*fn)(void *),const char *name,unsigned stack,void *arg,unsigned priority,TaskHandle_t *out) {
    (void)priority;if (out) *out=(void *)1;task_starts++;
    if (!strcmp(name,"mqtt_tx")) { assert(stack>=4096);mqtt_sender=fn;mqtt_sender_arg=arg; }
    else { assert(stack>=8192);fn(arg); }
    return pdPASS;
}
EventGroupHandle_t xEventGroupCreate(void) { return calloc(1, sizeof(*xEventGroupCreate())); }
EventBits_t xEventGroupClearBits(EventGroupHandle_t g, EventBits_t b) { g->bits &= ~b; return g->bits; }
EventBits_t xEventGroupSetBits(EventGroupHandle_t g, EventBits_t b) { g->bits |= b; return g->bits; }
EventBits_t xEventGroupWaitBits(EventGroupHandle_t g, EventBits_t b, int clear, int all, unsigned timeout) {
    (void)all; (void)timeout;
    unsigned result = g->bits;
    if (clear) g->bits &= ~b;
    return result;
}
SemaphoreHandle_t xSemaphoreCreateMutex(void) { return calloc(1, sizeof(*xSemaphoreCreateMutex())); }
int xSemaphoreTake(SemaphoreHandle_t s, unsigned timeout) { (void)timeout; assert(!s->taken); s->taken=1; return 1; }
int xSemaphoreGive(SemaphoreHandle_t s) { assert(s->taken); s->taken=0; return 1; }
esp_err_t esp_timer_create(const esp_timer_create_args_t *args, esp_timer_handle_t *out) {
    timer=calloc(1, sizeof(*timer)); timer->args=*args; *out=timer; return ESP_OK;
}
esp_err_t esp_timer_stop(esp_timer_handle_t t) { t->delay=0; return ESP_OK; }
esp_err_t esp_timer_start_once(esp_timer_handle_t t, uint64_t delay) { t->delay=delay; return ESP_OK; }
esp_err_t esp_event_loop_create_default(void) { return ESP_OK; }
esp_err_t esp_event_handler_register(esp_event_base_t base, int32_t id, event_callback_t cb, void *arg) {
    (void)id; (void)arg;
    if (!strcmp(base,WIFI_EVENT)) wifi_callback=cb; else ip_callback=cb;
    return ESP_OK;
}
esp_err_t esp_netif_init(void) { return ESP_OK; }
esp_netif_t *esp_netif_create_default_wifi_sta(void) { static esp_netif_t netif; return &netif; }
esp_err_t esp_netif_get_ip_info(esp_netif_t *netif, esp_netif_ip_info_t *ip) { (void)netif; *ip=(esp_netif_ip_info_t){0}; return ESP_OK; }
esp_err_t esp_wifi_init(const wifi_init_config_t *cfg) { (void)cfg; return ESP_OK; }
esp_err_t esp_wifi_set_storage(int storage) { assert(storage==WIFI_STORAGE_RAM); return ESP_OK; }
esp_err_t esp_wifi_set_mode(int mode) { (void)mode; return ESP_OK; }
esp_err_t esp_wifi_set_ps(int ps) { (void)ps; return ESP_OK; }
esp_err_t esp_wifi_set_config(int interface, const wifi_config_t *cfg) { (void)interface; wifi=*cfg; assert(cfg->sta.sae_pwe_h2e==WPA3_SAE_PWE_BOTH); return ESP_OK; }
esp_err_t esp_wifi_start(void) { radio_started=true; wifi_callback(NULL,WIFI_EVENT,WIFI_EVENT_STA_START,NULL); return ESP_OK; }
esp_err_t esp_wifi_stop(void) {
    radio_started=false; has_ip=false;
    wifi_event_sta_disconnected_t ev={.reason=WIFI_REASON_AUTH_FAIL};
    wifi_callback(NULL,WIFI_EVENT,WIFI_EVENT_STA_DISCONNECTED,&ev);
    wifi_callback(NULL,WIFI_EVENT,WIFI_EVENT_STA_STOP,NULL);
    return ESP_OK;
}
esp_err_t esp_wifi_connect(void) {
    assert(radio_started && wifi.sta.ssid[0]);
    assert(connects < 128);memcpy(attempted[connects],wifi.sta.ssid,32);connects++;
    bool list_case=!strncmp(scenario,"list_",5);
    bool accepted=false;
    if (list_case) {
        accepted=(!memcmp(wifi.sta.ssid,"Strong_AP",9) && !memcmp(wifi.sta.password,"strong-password",15)) ||
                 (!memcmp(wifi.sta.ssid,"Weak_AP",7) && !memcmp(wifi.sta.password,"mock-password",13)) ||
                 (!memcmp(wifi.sta.ssid,"Open_AP",7) && wifi.sta.password[0]==0 && wifi.sta.threshold.authmode==WIFI_AUTH_OPEN) ||
                 (!memcmp(wifi.sta.ssid,"12345678901234567890123456789012",32) && !memcmp(wifi.sta.password,"mock-password",13));
        if (!strcmp(scenario,"list_fallback") && !memcmp(wifi.sta.ssid,"Strong_AP",9)) accepted=false;
        if (!strcmp(scenario,"list_all_fail")) accepted=false;
    } else accepted=(!memcmp(wifi.sta.ssid,"Good_AP",7) || !memcmp(wifi.sta.ssid,"12345678901234567890123456789012",32)) &&
        !memcmp(wifi.sta.password,"mock-password",13);
    if (accepted) {
        has_ip=true;
        ip_event_got_ip_t ev={0}; ip_callback(NULL,IP_EVENT,IP_EVENT_STA_GOT_IP,&ev);
    } else {
        wifi_event_sta_disconnected_t ev={.reason=WIFI_REASON_AUTH_FAIL};
        wifi_callback(NULL,WIFI_EVENT,WIFI_EVENT_STA_DISCONNECTED,&ev);
    }
    return ESP_OK;
}
esp_err_t esp_wifi_scan_start(const wifi_scan_config_t *cfg, bool blocking) {
    (void)cfg;assert(blocking && radio_started);scans++;scan_offset=0;
    return !strcmp(scenario,"list_scan_failure") && scans==1 ? ESP_FAIL : ESP_OK;
}
esp_err_t esp_wifi_scan_get_ap_records(uint16_t *count, wifi_ap_record_t *records) {
    assert(*count>=1); *count=1;
    records[0]=(wifi_ap_record_t){.ssid="Good_AP",.rssi=-42,.primary=6,.authmode=WIFI_AUTH_WPA2_PSK};
    return ESP_OK;
}
esp_err_t esp_wifi_scan_get_ap_num(uint16_t *count) { *count=(uint16_t)scan_count;return ESP_OK; }
esp_err_t esp_wifi_scan_get_ap_record(wifi_ap_record_t *record) {
    assert(scan_offset<scan_count);
    if (!strcmp(scenario,"list_record_failure") && scans==1 && scan_offset==1) return ESP_FAIL;
    *record=scan_records[scan_offset++];return ESP_OK;
}
esp_err_t esp_wifi_clear_ap_list(void) { scan_offset=scan_count;return ESP_OK; }
bool esp_sntp_enabled(void) { return true; }
void esp_sntp_setoperatingmode(int mode) { (void)mode; }
void esp_sntp_set_time_sync_notification_cb(void (*cb)(struct timeval *)) { (void)cb; }
void esp_sntp_setservername(int index, const char *name) { (void)index; (void)name; }
void esp_sntp_init(void) {}
esp_err_t nvs_flash_init(void) { return ESP_OK; }
esp_err_t nvs_flash_erase(void) { return ESP_OK; }
esp_err_t nvs_open(const char *name, int mode, nvs_handle_t *out) { assert(!strcmp(name,"hrc_net")); (void)mode; *out=1; return ESP_OK; }
esp_err_t nvs_set_blob(nvs_handle_t h, const char *key, const void *value, size_t size) {
    (void)h; assert(size<=128);
    if (!strcmp(key,"wifi")) { memcpy(saved_wifi,value,size); saved_wifi_size=size; }
    else { assert(!strcmp(key,"broker")); memcpy(saved_broker,value,size); saved_broker_size=size; }
    return ESP_OK;
}
esp_err_t nvs_get_blob(nvs_handle_t h, const char *key, void *value, size_t *size) {
    (void)h;nvs_reads++;
    size_t available=!strcmp(key,"wifi") ? saved_wifi_size : saved_broker_size;
    if (!available) return ESP_ERR_NOT_FOUND;
    assert(*size>=available);
    memcpy(value,!strcmp(key,"wifi") ? saved_wifi : saved_broker,available);
    *size=available; return ESP_OK;
}
esp_err_t nvs_commit(nvs_handle_t h) { (void)h; commits++; return ESP_OK; }
void nvs_close(nvs_handle_t h) { (void)h; }
esp_mqtt_client_handle_t esp_mqtt_client_init(const esp_mqtt_client_config_t *cfg) {
    struct mqtt_stub *client=calloc(1,sizeof(*client));
    strcpy(client->uri,cfg->broker.address.uri); client->limit=cfg->outbox.limit;
    assert(has_ip);
    assert(cfg->session.keepalive>0 && cfg->session.message_retransmit_timeout>=5000);
    if (!strncmp(scenario,"list_",5)) assert(!strcmp(client->uri,"mqtt://macro-broker.test:1885"));
    mqtt_created++; last_client=client; return client;
}
esp_err_t esp_mqtt_client_register_event(esp_mqtt_client_handle_t c, int id, event_callback_t cb, void *arg) { (void)id; (void)arg; c->callback=cb; return ESP_OK; }
esp_err_t esp_mqtt_client_destroy(esp_mqtt_client_handle_t c) { free(c); return ESP_OK; }
esp_err_t esp_mqtt_client_start(esp_mqtt_client_handle_t c) {
    esp_mqtt_event_t ev={.client=c};
    assert(has_ip); c->callback(NULL,"mqtt",MQTT_EVENT_CONNECTED,&ev); return ESP_OK;
}
esp_err_t esp_mqtt_client_stop(esp_mqtt_client_handle_t c) { c->callback(NULL,"mqtt",MQTT_EVENT_DISCONNECTED,NULL); return ESP_OK; }
esp_err_t esp_mqtt_client_disconnect(esp_mqtt_client_handle_t c) {
    forced_disconnects++;c->callback(NULL,"mqtt",MQTT_EVENT_DISCONNECTED,NULL);return ESP_OK;
}
esp_err_t esp_mqtt_client_set_uri(esp_mqtt_client_handle_t c, const char *uri) { strcpy(c->uri,uri); return ESP_OK; }
int esp_mqtt_client_subscribe(esp_mqtt_client_handle_t c, const char *topic, int qos) {
    (void)topic;assert(qos==1);subscribes++;
    if (c->bytes>c->limit) { subscribe_failures++;return -2; }
    if (reject_subscribe) { reject_subscribe--;subscribe_failures++;return -1; }
    if (!defer_suback) {
        char grant=1;esp_mqtt_event_t ev={.client=c,.msg_id=(int)subscribes,.data=&grant,.data_len=1};
        c->callback(NULL,"mqtt",MQTT_EVENT_SUBSCRIBED,&ev);
    }
    return (int)subscribes;
}
int esp_mqtt_client_publish(esp_mqtt_client_handle_t c,const char *t,const char *p,int n,int qos,int retain) { return esp_mqtt_client_enqueue(c,t,p,n,qos,retain,1); }
int esp_mqtt_client_enqueue(esp_mqtt_client_handle_t c,const char *t,const char *p,int n,int qos,int retain,int store) {
    (void)p; assert(qos==1 && !retain && store);
    assert(!measurement_context && "telemetry enqueue must not take the SDK network mutex");
    sdk_enqueues++;
    if (reject_enqueue) { reject_enqueue--;return -1; }
    /* Match esp-mqtt 1.0.0: its limit check omits MQTT packet overhead. */
    if (c->bytes+n > c->limit) return -2;
    if (sdk_enqueues==1) strcpy(first_transfer_topic,t);
    strcpy(last_transfer_topic,t);
    assert(n<(int)sizeof(last_transfer_payload));
    memcpy(last_transfer_payload,p,(size_t)n);last_transfer_payload[n]=0;
    c->bytes+=n+(int)strlen(t)+8; return 1;
}
int esp_mqtt_client_get_outbox_size(esp_mqtt_client_handle_t c) {
    assert(!measurement_context && "outbox diagnostics must not take the SDK mutex");return c->bytes;
}

static void preload(const char *ssid,const char *password) {
    struct { char ssid[33],password[65]; } credentials={0};
    strcpy(credentials.ssid,ssid); strcpy(credentials.password,password);
    memcpy(saved_wifi,&credentials,sizeof(credentials)); saved_wifi_size=sizeof(credentials);
}
static void ap(const char *ssid,int rssi,int auth) {
    assert(scan_count<40);
    wifi_ap_record_t *record=&scan_records[scan_count++];
    memset(record,0,sizeof(*record));strcpy((char *)record->ssid,ssid);
    record->rssi=(int8_t)rssi;record->authmode=auth;record->primary=6;
}
static void test_list(void) {
    assert(hrc_wifi_uses_list());
    preload("Good_AP","wrong-password");
    strcpy((char *)saved_broker,"mqtt://stale.invalid:1883");saved_broker_size=strlen((char *)saved_broker)+1;
    if (!strcmp(scenario,"list_missing")) ap("Unknown_AP",-25,WIFI_AUTH_WPA2_PSK);
    else if (!strcmp(scenario,"list_record_failure")) {
        ap("Strong_AP",-35,WIFI_AUTH_WPA2_PSK);ap("Weak_AP",-70,WIFI_AUTH_WPA2_PSK);
    }
    else if (!strcmp(scenario,"list_many_aps")) {
        for (unsigned i=0;i<30;i++) { char name[32];snprintf(name,sizeof(name),"Other_%u",i);ap(name,-20,WIFI_AUTH_WPA2_PSK); }
        ap("Weak_AP",-90,WIFI_AUTH_WPA2_PSK);
    } else if (!strcmp(scenario,"list_full_ssid")) ap("12345678901234567890123456789012",-45,WIFI_AUTH_WPA2_PSK);
    else if (!strcmp(scenario,"list_duplicate")) ap("Strong_AP",-35,WIFI_AUTH_WPA2_PSK);
    else if (!strcmp(scenario,"list_open")) ap("Open_AP",-20,WIFI_AUTH_OPEN);
    else {
        ap("Unknown_AP",-10,WIFI_AUTH_WPA2_PSK);
        ap("Invalid_AP",-15,WIFI_AUTH_WPA2_PSK);
        ap("Weak_AP",-70,WIFI_AUTH_WPA2_PSK);
        ap("Strong_AP",-35,!strcmp(scenario,"list_unsupported") ? 99 : WIFI_AUTH_WPA2_PSK);
    }
    esp_err_t initialized=hrc_net_init();
    assert(nvs_reads==0 && commits==0 && scans==1 && task_starts==1);
    if (!strcmp(scenario,"list_missing") || !strcmp(scenario,"list_blank") || !strcmp(scenario,"list_invalid") ||
        !strcmp(scenario,"list_scan_failure") || !strcmp(scenario,"list_record_failure") || !strcmp(scenario,"list_all_fail")) {
        assert(initialized!=ESP_OK && !hrc_wifi_connected());
        if (!strcmp(scenario,"list_blank") || !strcmp(scenario,"list_invalid")) {
            assert(!hrc_wifi_configured() && connects==0 && timer->delay==0);return;
        }
        assert(hrc_wifi_configured() && timer->delay==1000000);
        if (!strcmp(scenario,"list_all_fail")) {
            assert(connects==2 && !strcmp(attempted[0],"Strong_AP") && !strcmp(attempted[1],"Weak_AP"));
            timer->args.callback(NULL);assert(scans==2 && connects==4 && timer->delay==2000000);return;
        }
        assert(connects==0);
        scan_count=0;ap("Weak_AP",-50,WIFI_AUTH_WPA2_PSK);
        timer->args.callback(NULL);
        assert(scans==2 && connects==1 && hrc_wifi_connected() && !strcmp(attempted[0],"Weak_AP"));
    } else {
        assert(initialized==ESP_OK && hrc_wifi_connected());
        if (!strcmp(scenario,"list_fallback") || !strcmp(scenario,"list_duplicate")) {
            assert(connects==2 && !strcmp(attempted[0],"Strong_AP"));
            assert(!strcmp(attempted[1],!strcmp(scenario,"list_duplicate") ? "Strong_AP" : "Weak_AP"));
        } else {
            assert(connects==1);
            const char *expected=!strcmp(scenario,"list_open") ? "Open_AP" :
                !strcmp(scenario,"list_full_ssid") ? "12345678901234567890123456789012" :
                (!strcmp(scenario,"list_many_aps") || !strcmp(scenario,"list_unsupported")) ? "Weak_AP" : "Strong_AP";
            assert(!strcmp(attempted[0],expected));
        }
    }
    assert(hrc_mqtt_start()==ESP_OK && hrc_mqtt_connected() && commits==0);
    if (!strcmp(scenario,"list_disconnect")) {
        has_ip=false;wifi_event_sta_disconnected_t event={.reason=WIFI_REASON_BEACON_TIMEOUT};
        wifi_callback(NULL,WIFI_EVENT,WIFI_EVENT_STA_DISCONNECTED,&event);
        assert(!hrc_wifi_connected() && timer->delay==1000000);
        scan_count=0;ap("Weak_AP",-50,WIFI_AUTH_WPA2_PSK);
        timer->args.callback(NULL);
        assert(scans==2 && connects==2 && hrc_wifi_connected() && !strcmp(attempted[1],"Weak_AP"));
    }
    assert(nvs_reads==0 && commits==0);
}
int main(int argc,char **argv) {
    assert(argc==2);
    const char *test=argv[1];scenario=test;
    if (!strncmp(test,"list_",5)) { test_list();return 0; }
    if (!strcmp(test,"backoff") || !strcmp(test,"scan_retry")) preload("Bad_AP","wrong-password");
    if (!strcmp(test,"saved_boot")) preload("Good_AP","mock-password");
    esp_err_t initialized=hrc_net_init();
    if (!strcmp(test,"backoff")) {
        assert(initialized==ESP_ERR_TIMEOUT);
        const unsigned delays[]={1000,2000,4000,8000,16000,30000,30000};
        for (size_t i=0;i<sizeof(delays)/sizeof(delays[0]);i++) {
            assert(timer->delay==delays[i]*1000ULL);
            timer->args.callback(timer->args.arg);
        }
        return 0;
    }
    if (!strcmp(test,"scan_retry")) {
        hrc_setup_ap_t aps[20]; size_t count;
        assert(hrc_wifi_scan(aps,20,&count)==ESP_OK && count==1 && scans==1);
        assert(!strcmp(aps[0].ssid,"Good_AP")); assert(!hrc_wifi_connected());
        assert(!strcmp((char *)saved_wifi,"Bad_AP")); return 0;
    }
    if (!strcmp(test,"saved_boot")) { assert(initialized==ESP_OK && hrc_wifi_connected()); return 0; }
    assert(initialized==ESP_ERR_NOT_FOUND && connects==0);
    assert(hrc_mqtt_start()==ESP_OK && mqtt_created==0);
    assert(hrc_mqtt_enqueue("t","data",4)==ESP_ERR_INVALID_STATE);
    if (!strcmp(test,"blank")) {
        hrc_setup_ap_t aps[20]; size_t count;
        assert(hrc_wifi_scan(aps,20,&count)==ESP_OK && count==1);
        assert(connects==0 && mqtt_created==0); return 0;
    }
    if (!strcmp(test,"broker_deferred")) {
        assert(hrc_mqtt_set_broker("mqtt://broker.local:1884")==ESP_OK);
        assert(mqtt_created==0 && !strcmp((char *)saved_broker,"mqtt://broker.local:1884"));
    }
    bool saved=false;
    const char *ssid=!strcmp(test,"full_ssid") ? "12345678901234567890123456789012" : "Good_AP";
    assert(hrc_wifi_connect(ssid,"mock-password",&saved)==ESP_OK && saved);
    assert(hrc_wifi_connected() && hrc_mqtt_connected() && mqtt_created==1);
    assert(!strcmp((char *)saved_wifi,ssid));
    if (!strcmp(test,"mqtt_nonblocking")) {
        measurement_context=true;
        assert(hrc_mqtt_enqueue("hrc/v1/devices/esp32_01/telemetry","data",4)==ESP_OK);
        (void)hrc_mqtt_outbox_size();
        assert(hrc_mqtt_publish("ack","ack",3)==ESP_OK);
        measurement_context=false;
        assert(sdk_enqueues==0);
    } else if (!strcmp(test,"mqtt_headroom")) {
        char payload[222];memset(payload,'x',sizeof(payload));
        for (unsigned i=0;i<500;i++) {
            (void)hrc_mqtt_enqueue("hrc/v1/devices/esp32_01/telemetry",payload,sizeof(payload));
            pump_sender(1);
        }
        assert(hrc_mqtt_outbox_size()<last_client->limit);
        assert(esp_mqtt_client_start(last_client)==ESP_OK);
        pump_sender(1);
        assert(subscribe_failures==0 && hrc_mqtt_control_ready());
        assert(hrc_mqtt_publish("hrc/v1/devices/esp32_01/ack","ack",3)==ESP_OK);
        pump_sender(1);
        assert(!strcmp(last_transfer_topic,"hrc/v1/devices/esp32_01/ack"));
        /* PUBACKs drain the SDK backlog; telemetry resumes without losing the
         * oldest RAM message or starving queued control ACKs. */
        last_client->bytes=0;
        unsigned before=sdk_enqueues;
        pump_sender(1);
        assert(sdk_enqueues>before);
    } else if (!strcmp(test,"mqtt_subscribe_retry")) {
        reject_subscribe=1;
        assert(hrc_mqtt_enqueue("t","data",4)==ESP_OK);
        pump_sender(1);
        assert(subscribe_failures==1 && !hrc_mqtt_control_ready() && sdk_enqueues==0);
        pump_sender(11);
        assert(subscribes==2 && hrc_mqtt_control_ready() && sdk_enqueues==1);
    } else if (!strcmp(test,"mqtt_suback_gate")) {
        defer_suback=true;
        assert(hrc_mqtt_enqueue("t","data",4)==ESP_OK);
        pump_sender(1);
        assert(!hrc_mqtt_control_ready() && sdk_enqueues==0);
        char grant=1;esp_mqtt_event_t ev={.client=last_client,.msg_id=(int)subscribes,.data=&grant,.data_len=1};
        last_client->callback(NULL,"mqtt",MQTT_EVENT_SUBSCRIBED,&ev);
        pump_sender(1);
        assert(hrc_mqtt_control_ready() && sdk_enqueues==1);
    } else if (!strcmp(test,"mqtt_offline")) {
        pump_sender(1);
        last_client->callback(NULL,"mqtt",MQTT_EVENT_DISCONNECTED,NULL);
        unsigned before=sdk_enqueues;
        assert(hrc_mqtt_enqueue("t","data",4)==ESP_OK);
        pump_sender(2);
        assert(sdk_enqueues==before && hrc_mqtt_pending_count()==1);
        assert(esp_mqtt_client_start(last_client)==ESP_OK);
        pump_sender(1);
        assert(hrc_mqtt_control_ready() && sdk_enqueues==before+1);
    } else if (!strcmp(test,"mqtt_puback_watchdog")) {
        assert(hrc_mqtt_enqueue("t","data",4)==ESP_OK);
        pump_sender(1);
        assert(forced_disconnects==0);
        pump_sender(150);
        assert(forced_disconnects==1 && !hrc_mqtt_connected());
        assert(esp_mqtt_client_start(last_client)==ESP_OK);
        last_client->bytes=0;
        pump_sender(1);
        assert(hrc_mqtt_control_ready());
    } else if (!strcmp(test,"mqtt_puback_progress")) {
        assert(hrc_mqtt_enqueue("t","data",4)==ESP_OK);
        pump_sender(1);
        pump_sender(100);
        esp_mqtt_event_t ev={.client=last_client,.msg_id=1};
        last_client->callback(NULL,"mqtt",MQTT_EVENT_PUBLISHED,&ev);
        pump_sender(100);
        assert(forced_disconnects==0 && hrc_mqtt_control_ready());
    } else if (!strcmp(test,"mqtt_suback_rejected")) {
        defer_suback=true;
        assert(hrc_mqtt_enqueue("t","data",4)==ESP_OK);
        pump_sender(1);
        char rejected=(char)0x80;esp_mqtt_event_t ev={.client=last_client,.msg_id=(int)subscribes,.data=&rejected,.data_len=1};
        last_client->callback(NULL,"mqtt",MQTT_EVENT_SUBSCRIBED,&ev);
        assert(!hrc_mqtt_control_ready() && sdk_enqueues==0);
        defer_suback=false;
        pump_sender(11);
        assert(hrc_mqtt_control_ready() && sdk_enqueues==1);
    } else if (!strcmp(test,"mqtt_suback_timeout")) {
        defer_suback=true;
        assert(hrc_mqtt_enqueue("t","data",4)==ESP_OK);
        pump_sender(151);
        assert(subscribes==2 && !hrc_mqtt_control_ready() && sdk_enqueues==0);
    } else if (!strcmp(test,"mqtt_sdk_retry")) {
        reject_enqueue=1;
        assert(hrc_mqtt_enqueue("t","data",4)==ESP_OK);
        pump_sender(1);
        assert(hrc_mqtt_pending_count()==1 && last_client->bytes==0);
        pump_sender(1);
        assert(hrc_mqtt_pending_count()==0 && sdk_enqueues==2);
    } else if (!strcmp(test,"mqtt_ack_priority")) {
        for (unsigned i=0;i<16;i++) assert(hrc_mqtt_enqueue("t","data",4)==ESP_OK);
        assert(hrc_mqtt_enqueue("t","data",4)==ESP_ERR_NO_MEM);
        assert(hrc_mqtt_publish("ack","ack",3)==ESP_OK);
        pump_sender(1);
        assert(hrc_mqtt_pending_count()==13 && last_client->bytes>0);
        assert(!strcmp(first_transfer_topic,"ack"));
    } else if (!strcmp(test,"mqtt_payload_copy")) {
        char payload[]="data";
        assert(hrc_mqtt_enqueue("t",payload,4)==ESP_OK);
        memset(payload,'x',4);
        pump_sender(1);
        assert(!strcmp(last_transfer_payload,"data") && hrc_mqtt_pending_count()==0);
    } else if (!strcmp(test,"mqtt_invalid")) {
        char oversized[385];memset(oversized,'a',sizeof(oversized));
        assert(hrc_mqtt_enqueue("t",oversized,sizeof(oversized))==ESP_ERR_INVALID_ARG);
        assert(hrc_mqtt_enqueue(NULL,"data",4)==ESP_ERR_INVALID_ARG);
        assert(hrc_mqtt_enqueue("","data",4)==ESP_ERR_INVALID_ARG);
        assert(hrc_mqtt_enqueue("t",NULL,4)==ESP_ERR_INVALID_ARG);
    } else if (!strcmp(test,"failed_join")) {
        unsigned committed=commits;
        assert(hrc_wifi_connect("Good_AP","wrong-password",&saved)==ESP_ERR_TIMEOUT);
        assert(!saved && commits==committed && hrc_wifi_connected());
        assert(!strcmp((char *)saved_wifi,"Good_AP"));
        assert(!strcmp((char *)saved_wifi+33,"mock-password"));
    } else if (!strcmp(test,"outbox")) {
        esp_err_t queued=ESP_OK;
        for (unsigned i=0;i<300 && queued==ESP_OK;i++) queued=hrc_mqtt_enqueue("t","data",4);
        assert(queued==ESP_ERR_NO_MEM && hrc_mqtt_outbox_size()<=10240);
    } else if (!strcmp(test,"broker_active")) {
        assert(hrc_mqtt_set_broker("mqtt://broker.local:1884")==ESP_OK);
        assert(hrc_mqtt_connected() && !strcmp((char *)saved_broker,"mqtt://broker.local:1884"));
    } else assert(!strcmp(test,"join") || !strcmp(test,"full_ssid") || !strcmp(test,"broker_deferred"));
    return 0;
}
