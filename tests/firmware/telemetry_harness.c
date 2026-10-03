/* Drive real sampling/control with a stalled MQTT mailbox and a fake clock. */
#include "idf_stub.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "hcsr04.h"
#include "telemetry.h"
#include "wifi_mqtt.h"
#include <assert.h>
#include <setjmp.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

static unsigned samples, logs, failures, ack_count, limit=25;
static bool invalid, too_far;
static int64_t clock_us;
static jmp_buf stop_sampling;
static char latest_log[512], latest_payload[384], latest_ack[384];
struct queue_stub { unsigned size; bool occupied; unsigned char data[256]; };

const char *esp_err_to_name(esp_err_t error) { (void)error;return "stub-error"; }
void stub_log(const char *format, ...) {
    char message[512];va_list args;va_start(args,format);
    vsnprintf(message,sizeof(message),format,args);va_end(args);
    if (!strncmp(message,"distance_mm=",12)) { logs++;strcpy(latest_log,message); }
}
QueueHandle_t xQueueCreate(unsigned count,unsigned size) {
    (void)count;assert(size<=256);QueueHandle_t q=calloc(1,sizeof(*q));q->size=size;return q;
}
int xQueueSend(QueueHandle_t q,const void *item,unsigned wait) {
    assert(wait==0);if (q->occupied) return pdFALSE;
    memcpy(q->data,item,q->size);q->occupied=true;return pdTRUE;
}
int xQueueReceive(QueueHandle_t q,void *item,unsigned wait) {
    assert(wait==0);if (!q->occupied) return pdFALSE;
    memcpy(item,q->data,q->size);q->occupied=false;return pdTRUE;
}
TickType_t xTaskGetTickCount(void) { return (TickType_t)(clock_us/1000); }
int xTaskDelayUntil(TickType_t *last,TickType_t interval) {
    if (samples>=limit) longjmp(stop_sampling,1);
    *last+=interval;clock_us=(int64_t)*last*1000;return pdTRUE;
}
int64_t esp_timer_get_time(void) { return clock_us; }
uint32_t esp_random(void) { return 42; }
bool hrc_time_synced(void) { return false; }
int64_t hrc_time_utc_ms(void) { return 0; }
bool hrc_wifi_connected(void) { return true; }
bool hrc_mqtt_connected(void) { return false; }
bool hrc_mqtt_control_ready(void) { return false; }
int hrc_mqtt_outbox_size(void) { return 30720; }
unsigned hrc_mqtt_pending_count(void) { return 16; }
hcsr04_result_t hcsr04_measure_once(void) {
    samples++;
    return (hcsr04_result_t){.start_monotonic_us=clock_us,.echo_us=invalid ? -1 : too_far ? 28000 : 1000,
                            .error=invalid ? HCSR04_ERR_STUCK_LOW : HCSR04_OK};
}
esp_err_t hrc_mqtt_enqueue(const char *topic,const char *payload,int len) {
    assert(strstr(topic,"/telemetry") && len<(int)sizeof(latest_payload));
    memcpy(latest_payload,payload,(size_t)len);latest_payload[len]=0;failures++;
    return ESP_ERR_NO_MEM;
}
esp_err_t hrc_mqtt_publish(const char *topic,const char *payload,int len) {
    assert(strstr(topic,"/ack") && len<(int)sizeof(latest_ack));
    memcpy(latest_ack,payload,(size_t)len);latest_ack[len]=0;ack_count++;return ESP_OK;
}
int main(int argc,char **argv) {
    assert(argc==2);
    invalid=!strcmp(argv[1],"invalid");too_far=!strcmp(argv[1],"too_far");
    telemetry_init();
    const char *command="{\"command\":\"session_start\",\"session_id\":\"test_session\"}";
    telemetry_handle_control("control",command,(int)strlen(command));
    if (!setjmp(stop_sampling)) telemetry_task(NULL);
    assert(samples==25 && failures==25 && ack_count==1);
    assert(strstr(latest_ack,"\"sequence\":1"));
#if CONFIG_HRC_DISTANCE_LOG_PERIOD_MS > 0
    assert(logs==3 && strstr(latest_log,"sequence=21"));
    if (invalid) {
        assert(strstr(latest_log,"distance_mm=null") && strstr(latest_log,"ECHO_STUCK_LOW"));
        assert(strstr(latest_payload,"\"valid\":false") && strstr(latest_payload,"\"distance_mm\":null"));
    } else if (too_far) {
        assert(strstr(latest_log,"distance_mm=null") && strstr(latest_log,"ECHO_TIMEOUT"));
        assert(strstr(latest_payload,"\"valid\":false"));
    } else {
        assert(strstr(latest_log,"distance_mm=171") && strstr(latest_log,"echo_us=1000"));
        assert(strstr(latest_payload,"\"distance_mm\":171"));
    }
#else
    assert(logs==0);
#endif
    char status[256];telemetry_status(status,sizeof(status));
    assert(strstr(status,"samples=25") && strstr(status,"enqueue_dropped=25"));
    return 0;
}
