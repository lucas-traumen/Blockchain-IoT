#include "idf_stub.h"
#include "hcsr04.h"
#include <assert.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>

enum { NO_ECHO, VALID, STUCK_HIGH, TIMEOUT_HIGH, TOO_LONG, RESET_FAIL, TRIG_FAIL };
static int mode, receiver_active, callback_done;
static unsigned receives, resets;
static SemaphoreHandle_t semaphore;
struct rmt_stub { rmt_rx_event_callbacks_t callbacks; rmt_symbol_word_t *buffer; };
static struct rmt_stub receiver;
void stub_log(const char *format, ...) { (void)format; }
const char *esp_err_to_name(esp_err_t error) { (void)error;return "stub-error"; }
SemaphoreHandle_t xSemaphoreCreateBinary(void) { semaphore=calloc(1,sizeof(*semaphore));return semaphore; }
void vSemaphoreDelete(SemaphoreHandle_t s) { free(s); }
int xSemaphoreGiveFromISR(SemaphoreHandle_t s, BaseType_t *woken) { s->taken=1;*woken=1;return 1; }
int xSemaphoreTake(SemaphoreHandle_t s,unsigned wait) {
    if (wait && mode!=NO_ECHO && mode!=RESET_FAIL) {
        receiver.buffer[0]=(rmt_symbol_word_t){.level0=0,.duration0=10,.level1=1,.duration1=mode==TOO_LONG ? 31000 : 1000};
        rmt_rx_done_event_data_t data={.num_symbols=1,.received_symbols=receiver.buffer};
        receiver_active=0;callback_done=1;
        receiver.callbacks.on_recv_done(&receiver,&data,NULL);
    }
    if (s->taken) { s->taken=0;return 1; }
    return 0;
}
esp_err_t gpio_config(const gpio_config_t *config) { (void)config;return ESP_OK; }
esp_err_t gpio_set_level(gpio_num_t pin,unsigned level) { assert(pin==CONFIG_HRC_TRIG_GPIO);return mode==TRIG_FAIL && level==1 ? ESP_FAIL : ESP_OK; }
int gpio_get_level(gpio_num_t pin) { assert(pin==CONFIG_HRC_ECHO_GPIO);return mode==STUCK_HIGH || (mode==TIMEOUT_HIGH && callback_done); }
esp_err_t rmt_new_rx_channel(const rmt_rx_channel_config_t *cfg,rmt_channel_handle_t *out) { assert(cfg->resolution_hz==1000000);*out=&receiver;return ESP_OK; }
esp_err_t rmt_rx_register_event_callbacks(rmt_channel_handle_t channel,const rmt_rx_event_callbacks_t *cb,void *ctx) { (void)ctx;channel->callbacks=*cb;return ESP_OK; }
esp_err_t rmt_enable(rmt_channel_handle_t channel) { (void)channel;return ESP_OK; }
esp_err_t rmt_disable(rmt_channel_handle_t channel) { (void)channel;resets++;if (mode==RESET_FAIL) return ESP_FAIL;receiver_active=0;return ESP_OK; }
esp_err_t rmt_del_channel(rmt_channel_handle_t channel) { (void)channel;return ESP_OK; }
esp_err_t rmt_receive(rmt_channel_handle_t channel,void *buffer,size_t length,const rmt_receive_config_t *cfg) {
    (void)length;assert(cfg->signal_range_max_ns==30000000);
    if (receiver_active) return ESP_ERR_INVALID_STATE;
    receiver_active=1;callback_done=0;channel->buffer=buffer;receives++;return ESP_OK;
}
int64_t esp_timer_get_time(void) { static int64_t clock;clock+=100000;return clock; }
void esp_rom_delay_us(unsigned delay) { assert(delay>=10); }
int main(int argc,char **argv) {
    assert(argc==2 && hcsr04_init()==ESP_OK);
    if (!strcmp(argv[1],"recovery")) mode=NO_ECHO;
    else if (!strcmp(argv[1],"high_timeout")) mode=TIMEOUT_HIGH;
    else if (!strcmp(argv[1],"stuck_high")) mode=STUCK_HIGH;
    else if (!strcmp(argv[1],"too_long")) mode=TOO_LONG;
    else if (!strcmp(argv[1],"trig_recovery")) mode=TRIG_FAIL;
    else { assert(!strcmp(argv[1],"reset_failure"));mode=RESET_FAIL; }
    hcsr04_result_t result=hcsr04_measure_once();
    assert(result.echo_us==-1 && result.start_monotonic_us>0);
    if (mode==NO_ECHO) {
        assert(result.error==HCSR04_ERR_STUCK_LOW && resets==1);
        mode=VALID; result=hcsr04_measure_once();
        assert(result.error==HCSR04_OK && result.echo_us==1000 && receives==2);
    } else if (mode==STUCK_HIGH) assert(result.error==HCSR04_ERR_STUCK_HIGH && receives==0);
    else if (mode==RESET_FAIL) {
        assert(result.error==HCSR04_ERR_TRIG_FAIL);
        mode=VALID;
        assert(hcsr04_measure_once().error==HCSR04_ERR_TRIG_FAIL);
        result=hcsr04_measure_once();
        assert(result.error==HCSR04_OK && result.echo_us==1000);
    } else if (mode==TRIG_FAIL) {
        assert(result.error==HCSR04_ERR_TRIG_FAIL && resets==1);
        mode=VALID; result=hcsr04_measure_once();
        assert(result.error==HCSR04_OK && result.echo_us==1000);
    } else assert(result.error==HCSR04_ERR_TIMEOUT);
    return 0;
}
