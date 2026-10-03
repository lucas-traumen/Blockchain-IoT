/*
 * HRC Safety Log — driver HC-SR04 (ESP-IDF v5.x) — M4b
 *
 * Cách đo: RMT RX (đo cạnh xung bằng phần cứng), KHÔNG dùng GPIO + esp_timer.
 * Lý do:
 *   - Độ phân giải 1 µs (resolution_hz = 1 MHz): thời điểm cạnh được đóng
 *     gói bằng phần cứng, không phụ thuộc jitter scheduler FreeRTOS hay độ
 *     trễ ISR như khi polling GPIO bằng esp_timer_get_time() trong task.
 *   - Không busy-wait chiếm CPU suốt tối đa CONFIG_HRC_ECHO_TIMEOUT_MS mỗi
 *     lần đo — quan trọng khi M4c/M4d thêm stack Wi-Fi/MQTT và app loop.
 *   - Idle threshold của RMT (signal_range_max_ns) tự kết thúc phiên nhận
 *     khi tín hiệu giữ nguyên mức quá lâu, nên "echo lên cao rồi không hạ
 *     trong timeout" được phát hiện (phân loại ECHO_TIMEOUT) mà không cần
 *     timer phụ.
 * Đo đồng bộ (blocking): phiên nhận báo xong qua semaphore nội bộ; callback
 * recv_done chỉ nằm trong file này, API cho task ngoài là hàm thường (M4d
 * gọi hcsr04_measure_once() từ task đo riêng).
 *
 * Ngữ nghĩa lỗi (đối chiếu schemas/telemetry_v1.schema.json — error_code):
 *   HCSR04_ERR_TRIG_FAIL  : không điều khiển được chân TRIG (gpio_set_level
 *                           lỗi), hoặc không khởi động được phiên nhận RMT.
 *   HCSR04_ERR_STUCK_LOW  : sau TRIG, ECHO không có cạnh lên nào trong
 *                           timeout (cảm biến không nối/không đáp ứng).
 *   HCSR04_ERR_STUCK_HIGH : ECHO đang cao TRƯỚC khi đo (pre-condition) —
 *                           duy nhất trường hợp này.
 *   HCSR04_ERR_TIMEOUT    : ECHO có cạnh lên nhưng KHÔNG có cạnh xuống
 *                           trước khi hết timeout (echo không hoàn tất,
 *                           gồm cả xung "không có vật" ~38 ms chuẩn
 *                           HC-SR04). Ở mức driver lỗi này LUÔN có
 *                           echo_us = -1: độ rộng thật không biết (giá trị
 *                           bị idle threshold cắt là clamp, không phải số
 *                           đo). Khác với ECHO_TIMEOUT mức telemetry
 *                           (M4d: echo hoàn tất 26.2–30 ms nhưng
 *                           distance > 4500 mm) — trường hợp đó GIỮ
 *                           echo_us thật.
 * Khi lỗi: echo_us = -1. Khi OK: echo_us >= 1 (µs, tick RMT = 1 µs).
 * Driver KHÔNG quy đổi distance_mm (M4d làm, dùng CONFIG_HRC_SOUND_SPEED_M_S).
 */

#include "hcsr04.h"
#include "diagnostics.h"

#include <stddef.h>

#include "sdkconfig.h"

#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

#include "esp_attr.h"
#include "esp_err.h"
#include "esp_log.h"
#include "esp_rom_sys.h"
#include "esp_timer.h"

#include "driver/gpio.h"
#include "driver/rmt_common.h"
#include "driver/rmt_rx.h"

static const char *TAG = "hcsr04";
static hrc_fault_state_t s_reset_fault;

/* RMT tick = 1 µs: độ phân giải của độ rộng echo trả về. */
#define HCSR04_RMT_RESOLUTION_HZ 1000000u

/* Lọc nhiễu: bỏ xung ngắn hơn 1 µs (signal_range_min_ns). */
#define HCSR04_SIGNAL_RANGE_MIN_NS 1000u

/* Số symbol RMT mỗi phiên nhận. Xung echo = 1 cạnh lên + 1 cạnh xuống;
 * noise filter có thể tách thêm vài symbol nên 8 là dư sức. */
#define HCSR04_RX_MAX_SYMBOLS 8

/* RMT mem_block_symbols tối thiểu trên ESP32 classic (không dùng DMA). */
#define HCSR04_RMT_MEM_SYMBOLS 64

/* Grace chờ phiên nhận kết thúc: phiên tự kết thúc chậm nhất ≈ 2 × timeout
 * (cạnh xuống cuối hoặc idle threshold sau cạnh lên kẹt), cộng thêm 2 tick
 * FreeRTOS làm margin; tổng grace = 3 × timeout + 2 tick. */
#define HCSR04_SEM_EXTRA_TICKS 2

/* Độ rộng xung TRIG (µs) — HC-SR04 yêu cầu >= 10 µs. */
#define HCSR04_TRIG_PULSE_US 10u

static bool s_rx_enabled;
static rmt_channel_handle_t s_rx_chan;   /* NULL = chưa init */
static SemaphoreHandle_t s_done_sem;     /* give đúng 1 lần mỗi phiên nhận */
static rmt_symbol_word_t s_rx_symbols[HCSR04_RX_MAX_SYMBOLS]; /* RAM nội bộ */
static volatile size_t s_rx_num_symbols; /* số symbol phiên vừa kết thúc */

/*
 * ISR khi phiên nhận RMT kết thúc: chép symbol ra buffer tĩnh rồi báo task.
 * (Chỉ gọi khi có phiên đang chạy; semaphore tạo trước khi register.)
 */
static bool IRAM_ATTR hcsr04_rx_done_cb(rmt_channel_handle_t rx_chan,
                                        const rmt_rx_done_event_data_t *edata,
                                        void *user_ctx)
{
    (void)rx_chan;
    (void)user_ctx;

    BaseType_t task_woken = pdFALSE;

    size_t num = edata->num_symbols;
    if (num > HCSR04_RX_MAX_SYMBOLS)
    {
        num = HCSR04_RX_MAX_SYMBOLS;
    }
    for (size_t i = 0; i < num; i++)
    {
        s_rx_symbols[i] = edata->received_symbols[i];
    }
    s_rx_num_symbols = num;

    xSemaphoreGiveFromISR(s_done_sem, &task_woken);
    return task_woken == pdTRUE;
}

esp_err_t hcsr04_init(void)
{
    if (s_rx_chan != NULL)
    {
        return ESP_OK; /* đã init rồi */
    }

    if (CONFIG_HRC_TRIG_GPIO == CONFIG_HRC_ECHO_GPIO)
    {
        ESP_LOGE(TAG, "TRIG và ECHO không được cấu hình trùng GPIO (%d)",
                 CONFIG_HRC_TRIG_GPIO);
        return ESP_ERR_INVALID_ARG;
    }

    /* Semaphore đồng bộ phiên nhận (tạo trước khi register callback). */
    s_done_sem = xSemaphoreCreateBinary();
    if (s_done_sem == NULL)
    {
        return ESP_ERR_NO_MEM;
    }

    /* TRIG: output, kéo thấp (mức idle). */
    gpio_config_t trig_cfg = {
        .pin_bit_mask = 1ULL << CONFIG_HRC_TRIG_GPIO,
        .mode = GPIO_MODE_OUTPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    esp_err_t err = gpio_config(&trig_cfg);
    if (err != ESP_OK)
    {
        goto fail;
    }
    err = gpio_set_level((gpio_num_t)CONFIG_HRC_TRIG_GPIO, 0);
    if (err != ESP_OK)
    {
        goto fail;
    }

    /* ECHO: input (RMT RX channel sẽ claim GPIO và gắn vào matrix). */
    gpio_config_t echo_cfg = {
        .pin_bit_mask = 1ULL << CONFIG_HRC_ECHO_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_DISABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    err = gpio_config(&echo_cfg);
    if (err != ESP_OK)
    {
        goto fail;
    }

    /* RMT RX: 1 tick = 1 µs, ghi nhận cạnh xung bằng phần cứng. */
    rmt_rx_channel_config_t rx_cfg = {
        .gpio_num = CONFIG_HRC_ECHO_GPIO,
        .clk_src = RMT_CLK_SRC_DEFAULT,
        .resolution_hz = HCSR04_RMT_RESOLUTION_HZ,
        .mem_block_symbols = HCSR04_RMT_MEM_SYMBOLS,
    };
    err = rmt_new_rx_channel(&rx_cfg, &s_rx_chan);
    if (err != ESP_OK)
    {
        goto fail;
    }

    const rmt_rx_event_callbacks_t cbs = {
        .on_recv_done = hcsr04_rx_done_cb,
    };
    err = rmt_rx_register_event_callbacks(s_rx_chan, &cbs, NULL);
    if (err != ESP_OK)
    {
        goto fail_chan;
    }

    err = rmt_enable(s_rx_chan);
    if (err != ESP_OK)
    {
        goto fail_chan;
    }

    s_rx_enabled = true;
    ESP_LOGI(TAG, "HC-SR04 ready: TRIG=GPIO%d ECHO=GPIO%d timeout=%d ms",
             CONFIG_HRC_TRIG_GPIO, CONFIG_HRC_ECHO_GPIO,
             CONFIG_HRC_ECHO_TIMEOUT_MS);
    return ESP_OK;

fail_chan:
    rmt_del_channel(s_rx_chan);
    s_rx_chan = NULL;
fail:
    vSemaphoreDelete(s_done_sem);
    s_done_sem = NULL;
    return err;
}

/* Recover unfinished receives, including a TRIG error before recv_done. */
static esp_err_t reset_receiver(void)
{
    esp_err_t err = ESP_OK;
    if (s_rx_enabled) {
        err = rmt_disable(s_rx_chan);
        if (err == ESP_OK) s_rx_enabled = false;
    }
    if (err == ESP_OK) {
        err = rmt_enable(s_rx_chan);
        if (err == ESP_OK) s_rx_enabled = true;
    }
    hrc_fault_action_t action = hrc_fault_update(&s_reset_fault, (uint32_t)err,
                                                esp_timer_get_time());
    if (err != ESP_OK && action == HRC_FAULT_REPORT)
        ESP_LOGE(TAG, "RMT reset failed: %s (consecutive=%lu)",
                 esp_err_to_name(err), (unsigned long)s_reset_fault.consecutive);
    else if (action == HRC_FAULT_RECOVERED)
        ESP_LOGI(TAG, "RMT receiver reset recovered");
    return err;
}

hcsr04_result_t hcsr04_measure_once(void)
{
    hcsr04_result_t result = {
        .error = HCSR04_OK,
        .echo_us = -1,
        .start_monotonic_us = 0,
    };

    if (s_rx_chan == NULL || s_done_sem == NULL)
    {
        /* Chưa init: không có cấu hình TRIG/ECHO hợp lệ để điều khiển. */
        ESP_LOGE(TAG, "hcsr04_measure_once() gọi trước hcsr04_init()");
        result.error = HCSR04_ERR_TRIG_FAIL;
        return result;
    }

    /* Pre-condition: ECHO phải đang thấp trước khi đo; đang cao → kẹt cao
     * ("ECHO đang ở mức cao trước khi đo"). Mốc thời gian vẫn được chốt
     * trước khi trả lỗi để M4d dùng cho mẫu invalid này. */
    if (gpio_get_level((gpio_num_t)CONFIG_HRC_ECHO_GPIO) != 0)
    {
        result.start_monotonic_us = esp_timer_get_time();
        result.error = HCSR04_ERR_STUCK_HIGH;
        return result;
    }

    /* Dọn semaphore còn sót từ phiên trước (nếu có) để kết quả đọc luôn
     * thuộc phiên này. Điều kiện vận hành: không gọi hàm này dày hơn
     * ~2 × CONFIG_HRC_ECHO_TIMEOUT_MS (phiên cũ đã kết thúc trước đó). */
    (void)xSemaphoreTake(s_done_sem, 0);

    /* Bắt đầu phiên nhận. Idle giữ nguyên mức quá signal_range_max_ns
     * (= CONFIG_HRC_ECHO_TIMEOUT_MS) sẽ kết thúc phiên. Khi ECHO luôn thấp
     * (không có cạnh nào) phiên KHÔNG tự kết thúc — xử lý ở nhánh timeout
     * semaphore bên dưới. Phân loại lỗi làm SAU khi phiên kết thúc: dựa
     * vào mức ECHO cuối và tổng thời gian mức cao của symbol.
     * Ghi chú: với tick RMT 1 µs, trường duration bão hòa ở 32767 µs; chỉ
     * gặp khi timeout được cấu hình > ~32 ms, không ảnh hưởng việc phân
     * loại lỗi. */
    rmt_receive_config_t recv_cfg = {
        .signal_range_min_ns = HCSR04_SIGNAL_RANGE_MIN_NS,
        .signal_range_max_ns = (uint32_t)CONFIG_HRC_ECHO_TIMEOUT_MS * 1000000u,
    };
    s_rx_num_symbols = 0;
    if (!s_rx_enabled && reset_receiver() != ESP_OK) {
        result.start_monotonic_us = esp_timer_get_time();
        result.error = HCSR04_ERR_TRIG_FAIL;
        return result;
    }
    if (rmt_receive(s_rx_chan, s_rx_symbols, sizeof(s_rx_symbols), &recv_cfg) != ESP_OK)
    {
        /* Không khởi động được phiên nhận (ví dụ phiên trước chưa kết thúc
         * khi gọi dày hơn 2 × timeout) — chuỗi đo không phát được: TRIG_FAIL. */
        result.start_monotonic_us = esp_timer_get_time();
        result.error = HCSR04_ERR_TRIG_FAIL;
        (void)reset_receiver();
        return result;
    }

    /* Chốt mốc thời gian NGAY TRƯỚC khi phát TRIG (M4d dùng làm monotonic_us). */
    result.start_monotonic_us = esp_timer_get_time();

    /* Xung TRIG 10 µs. */
    if (gpio_set_level((gpio_num_t)CONFIG_HRC_TRIG_GPIO, 1) != ESP_OK)
    {
        result.error = HCSR04_ERR_TRIG_FAIL;
        (void)gpio_set_level((gpio_num_t)CONFIG_HRC_TRIG_GPIO, 0);
        (void)reset_receiver();
        return result;
    }
    esp_rom_delay_us(HCSR04_TRIG_PULSE_US);
    if (gpio_set_level((gpio_num_t)CONFIG_HRC_TRIG_GPIO, 0) != ESP_OK)
    {
        result.error = HCSR04_ERR_TRIG_FAIL;
        (void)reset_receiver();
        return result;
    }

    /* Chờ phiên nhận kết thúc. Phiên tự kết thúc chậm nhất ≈ 2 × timeout
     * (echo có cạnh lên nhưng không hạ: kết thúc tại cạnh lên + range_max),
     * nên grace = 3 × timeout + 2 tick là đủ; callback thường đến sớm hơn. */
    TickType_t wait_ticks =
        pdMS_TO_TICKS((TickType_t)CONFIG_HRC_ECHO_TIMEOUT_MS * 3u) +
        HCSR04_SEM_EXTRA_TICKS;
    if (xSemaphoreTake(s_done_sem, wait_ticks) != pdTRUE)
    {
        /* Callback chưa đến (bất thường — thường là line không có cạnh nào
         * sau TRIG nên phiên nhận không bao giờ kết thúc). Phân loại theo
         * mức ECHO hiện tại: cao = đã có cạnh lên nhưng không có cạnh xuống
         * trong timeout → ECHO_TIMEOUT; thấp = không có cạnh lên nào →
         * ECHO_STUCK_LOW. */
        const int echo_level = gpio_get_level((gpio_num_t)CONFIG_HRC_ECHO_GPIO);
        result.error =
            (echo_level != 0) ? HCSR04_ERR_TIMEOUT : HCSR04_ERR_STUCK_LOW;

        /* Reset receiver: khi line không có cạnh nào, phiên nhận không kết
         * thúc (không có recv_done) nên receiver còn kẹt trạng thái RUN —
         * lần gọi rmt_receive() sau sẽ trả ESP_ERR_INVALID_STATE mãi mãi.
         * disable → enable đưa channel về trạng thái khởi động được phiên
         * mới, chu kỳ đo sau chạy lại bình thường. */
        if (reset_receiver() != ESP_OK) result.error = HCSR04_ERR_TRIG_FAIL;
        else ESP_LOGD(TAG, "no recv_done, receiver reset (ECHO=%d)", echo_level);
        return result;
    }

    /* Phân loại sau khi phiên nhận kết thúc. Đọc mức ECHO TRƯỚC TIÊN: phiên
     * kết thúc do idle threshold khi ECHO vẫn giữ cao (bị cắt tại đúng mốc
     * timeout) — echo có cạnh lên nhưng KHÔNG có cạnh xuống trong timeout,
     * độ rộng thật không biết (giá trị trong symbol bị idle threshold cắt
     * là clamp, không phải số đo) → ECHO_TIMEOUT, echo_us = -1. Gồm cả xung
     * "không có vật" ~38 ms chuẩn HC-SR04. KHÔNG dùng giá trị pulse bị cắt
     * làm số đo. */
    if (gpio_get_level((gpio_num_t)CONFIG_HRC_ECHO_GPIO) != 0)
    {
        result.error = HCSR04_ERR_TIMEOUT;
        return result;
    }

    /* ECHO đã thấp (echo hoàn tất): duyệt symbol đếm tổng thời gian mức cao. */
    uint32_t high_us = 0;
    for (size_t i = 0; i < s_rx_num_symbols; i++)
    {
        if (s_rx_symbols[i].level0 == 1)
        {
            high_us += s_rx_symbols[i].duration0;
        }
        if (s_rx_symbols[i].level1 == 1)
        {
            high_us += s_rx_symbols[i].duration1;
        }
    }

    if (high_us == 0)
    {
        /* Không có cạnh lên nào (0 symbol cũng rơi vào đây): ECHO không lên
         * cao sau TRIG. */
        result.error = HCSR04_ERR_STUCK_LOW;
        return result;
    }

    const int64_t timeout_us = (int64_t)CONFIG_HRC_ECHO_TIMEOUT_MS * 1000;
    if ((int64_t)high_us > timeout_us)
    {
        /* Defensive: có cả hai cạnh nhưng độ rộng vượt timeout (cạnh xuống
         * xảy ra SAU khi hết timeout, phiên kết thúc trong khoảng grace) —
         * xung dài hơn thời gian cho phép. */
        result.error = HCSR04_ERR_TIMEOUT;
        return result;
    }

    result.echo_us = (int32_t)high_us;
    return result;
}
