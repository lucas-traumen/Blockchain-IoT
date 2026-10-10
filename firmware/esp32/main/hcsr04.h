/*
 * HRC Safety Log — driver HC-SR04 (ESP-IDF v5.x) — M4b
 *
 * Public API cho một lần đo độ rộng xung echo của HC-SR04.
 * Driver chỉ trả kết quả đo thô (echo_us + error + mốc thời gian);
 * việc quy đổi sang distance_mm do M4d thực hiện bằng
 * CONFIG_HRC_SOUND_SPEED_M_S.
 */
#pragma once

#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Lỗi của một lần đo — map 1-1 với error_code trong
 * schemas/telemetry_v1.schema.json:
 *   HCSR04_OK              -> null (mẫu valid)
 *   HCSR04_ERR_TRIG_FAIL   -> "TRIG_FAIL"
 *   HCSR04_ERR_STUCK_LOW   -> "ECHO_STUCK_LOW"
 *   HCSR04_ERR_STUCK_HIGH  -> "ECHO_STUCK_HIGH"
 *   HCSR04_ERR_TIMEOUT     -> "ECHO_TIMEOUT"
 */
typedef enum
{
    HCSR04_OK = 0,          /**< Đo thành công, echo_us >= 1. */
    HCSR04_ERR_TRIG_FAIL,   /**< Không điều khiển được chân TRIG (lỗi gpio). */
    HCSR04_ERR_STUCK_LOW,   /**< Sau TRIG, ECHO không có cạnh lên nào trong timeout. */
    HCSR04_ERR_STUCK_HIGH,  /**< ECHO đang cao TRƯỚC khi đo (pre-condition) —
                                 duy nhất trường hợp này. */
    HCSR04_ERR_TIMEOUT,     /**< ECHO có cạnh lên nhưng không có cạnh xuống
                                 trước khi hết timeout; mức driver luôn
                                 echo_us = -1 (độ rộng thật không biết —
                                 giá trị bị idle threshold cắt là clamp). */
} hcsr04_error_t;

/*
 * Phân biệt hai tầng ECHO_TIMEOUT (cùng error_code trong schema):
 *   - Mức driver (enum này): echo KHÔNG hoàn tất — có cạnh lên nhưng không
 *     có cạnh xuống trước hết timeout (gồm cả xung "không có vật" ~38 ms
 *     chuẩn HC-SR04) → LUÔN echo_us = -1, telemetry publish echo_us = null.
 *   - Mức telemetry (M4d trong telemetry.c): echo HOÀN TẤT 26.2–30 ms nhưng
 *     distance_mm quy đổi > 4500 (ngoài miền schema) → error_code
 *     "ECHO_TIMEOUT" GIỮ echo_us thật (số đo không bị cắt).
 */

/* Kết quả một lần đo HC-SR04. */
typedef struct
{
    hcsr04_error_t error;
    /* Độ rộng xung echo (µs, tick RMT = 1 µs); -1 khi không đo được. */
    int32_t echo_us;
    /* esp_timer_get_time() (µs) chốt ngay trước bước phát TRIG; các đường
     * lỗi trả về sớm vẫn có mốc hợp lệ để M4d dùng làm monotonic_us cho
     * mọi mẫu (kể cả mẫu invalid). */
    int64_t start_monotonic_us;
} hcsr04_result_t;

/*
 * Cấu hình phần cứng từ Kconfig:
 *   - TRIG = CONFIG_HRC_TRIG_GPIO: output, kéo thấp (mức idle của HC-SR04).
 *   - ECHO = CONFIG_HRC_ECHO_GPIO: input, gắn vào RMT RX channel (1 tick = 1 µs).
 * Gọi một lần trước hcsr04_measure_once(). Gọi lại khi đã init trả ESP_OK
 * (không tạo trùng tài nguyên).
 */
esp_err_t hcsr04_init(void);

/*
 * Thực hiện MỘT lần đo (blocking): phát xung TRIG 10 µs, chờ đo độ rộng
 * xung ECHO với timeout CONFIG_HRC_ECHO_TIMEOUT_MS.
 *
 * Hàm đồng bộ, dành cho task đo riêng (M4d); callback RMT chỉ nằm trong
 * driver, không lộ ra ngoài. KHÔNG re-entrant: chỉ một task được gọi tại
 * một thời điểm, và không gọi dày hơn ~2 × CONFIG_HRC_ECHO_TIMEOUT_MS.
 */
hcsr04_result_t hcsr04_measure_once(void);

#ifdef __cplusplus
}
#endif
