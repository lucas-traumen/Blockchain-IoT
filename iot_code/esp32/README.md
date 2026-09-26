# HRC Safety Log — Firmware ESP32 (hoàn chỉnh, 4/4 giai đoạn M4a–M4d)

Firmware ESP-IDF cho thiết bị đo khoảng cách HC-SR04, publish telemetry qua
MQTT (QoS1) và nhận lệnh session control từ backend. Firmware hoàn chỉnh cả
4 giai đoạn:

- **M4a** — khung project: CMake, Kconfig (`HRC_*`), `sdkconfig.defaults`,
  `app_main.c`.
- **M4b** — driver HC-SR04: đo độ rộng xung echo bằng RMT RX, độ phân giải
  1 µs (`hcsr04.c/.h`).
- **M4c** — mạng: Wi-Fi STA + SNTP (UTC) + MQTT client QoS1, tự reconnect
  (`wifi_mqtt.c/.h`).
- **M4d** — payload telemetry_v1 + session control/ack + app loop định kỳ
  `CONFIG_HRC_SAMPLE_PERIOD_MS` (`telemetry.c/.h`).

Luồng dữ liệu đầy đủ:

```
HC-SR04 → hcsr04 (RMT RX) → telemetry (payload v1) → wifi_mqtt → MQTT QoS1
                              ↑ mailbox (queue) ← control_v1 ← broker
                              └→ ack_v1 → wifi_mqtt → MQTT QoS1
```

- Đo → build payload 11 field theo `schemas/telemetry_v1.schema.json` →
  publish topic `hrc/v1/devices/<device_id>/telemetry` (QoS1, retain=false).
- Nhận lệnh `control_v1` trên topic `.../control` → mailbox (queue depth 4)
  → ack 9 field theo `schemas/ack_v1.schema.json` lên topic `.../ack`
  (QoS1). Ranh giới session backend lấy từ ack này.

## Trạng thái kiểm chứng (đọc kỹ trước khi dùng)

- **Target ESP-IDF: v5.5.** Chưa xác minh với phiên bản khác.
- **CHƯA build/flash được trên máy phát triển hiện tại** (không cài toolchain
  ESP-IDF). Mọi thứ dưới đây **chưa kiểm chứng trên phần cứng**:
  - `idf.py build` chưa chạy lần nào — firmware hoàn chỉnh về mã nguồn nhưng
    chưa qua một lần compile thật;
  - chưa flash, chưa chạy `app_main` trên board thật;
  - chưa đo thời lượng thật của một chu kỳ (đo blocking + build JSON +
    publish) so với `CONFIG_HRC_SAMPLE_PERIOD_MS`;
  - chưa chạy với broker MQTT thật, chưa xác minh SNTP sync thật, chưa xác
    minh parser control với payload thật của backend;
  - cấu hình Kconfig/sdkconfig chưa được trình kconfiggen xử lý thật.
- Các payload JSON ví dụ trong tài liệu là sản sinh lý thuyết từ code,
  chưa qua broker/schema validator thật.
- File C/Kconfig được viết theo chuẩn ESP-IDF v5.x nhưng cần build thật
  trước khi tin vào kết quả.

## Cách build (khi có ESP-IDF v5.5)

```bash
. $IDF_PATH/export.sh          # hoặc export.bat trên Windows
idf.py set-target esp32
idf.py menuconfig              # đặt HRC_* trong "HRC Safety Log Firmware"
idf.py build
idf.py -p PORT flash monitor   # PORT ví dụ /dev/ttyUSB0
```

Sau khi sửa `sdkconfig.defaults`, xóa `sdkconfig` để mặc định được áp dụng lại.

`sdkconfig.defaults` đặt `CONFIG_LWIP_SNTP_MAX_SERVERS=3` để SNTP có 3
server (`pool.ntp.org` + `time.google.com` + `time.cloudflare.com`); nếu
không đặt, lwIP mặc định chỉ biên dịch 1 server — 2 server dự phòng trong
`wifi_mqtt.c` bị compile-out.

## Option Kconfig (menu "HRC Safety Log Firmware")

| Option                  | Kiểu   | Default                        | Ý nghĩa |
| ----------------------- | ------ | ------------------------------ | ------- |
| `HRC_WIFI_SSID`         | string | (rỗng)                         | SSID Wi-Fi STA (M4c) |
| `HRC_WIFI_PASS`         | string | (rỗng, password)               | Mật khẩu Wi-Fi (M4c) |
| `HRC_MQTT_URI`          | string | `mqtt://192.168.1.10:1883`     | Broker MQTT (M4c), topic `hrc/v1/devices/<device_id>/telemetry` QoS1 |
| `HRC_DEVICE_ID`         | string | `esp32_01`                     | Định danh thiết bị, khớp `configs/sensor.yaml` |
| `HRC_SAMPLE_PERIOD_MS`  | int    | `100`                          | Chu kỳ đo/publish (ms), khớp `sample_period_ms` |
| `HRC_TRIG_GPIO`         | int    | `4`                            | GPIO TRIG HC-SR04 (output, range 0–33; tránh GPIO 6–11 nối flash), khớp `trig_gpio` |
| `HRC_ECHO_GPIO`         | int    | `5`                            | GPIO ECHO HC-SR04 (range 0–39; tránh GPIO 6–11 nối flash), khớp `echo_gpio` |
| `HRC_ECHO_TIMEOUT_MS`   | int    | `30`                           | Timeout chờ xung echo (range 1–30, khớp `echo_us` max 30000 µs của schema), khớp `echo_timeout_ms` |
| `HRC_SOUND_SPEED_M_S`   | int    | `343`                          | Tốc độ âm quy đổi khoảng cách (m/s), khớp `sound_speed_m_s` |

Default cảm biến khớp `configs/sensor.yaml`; payload telemetry bám
`schemas/telemetry_v1.schema.json`, ack bám `schemas/ack_v1.schema.json`.

## Cấu trúc module (cuối cùng)

```
iot_code/esp32/
├── CMakeLists.txt          # project-level (M4a)
├── sdkconfig.defaults      # mặc định build (M4a)
├── README.md               # (M4a, cập nhật M4d)
└── main/
    ├── CMakeLists.txt      # đăng ký component main — SRCS đầy đủ sau M4d
    ├── Kconfig.projbuild   # option HRC_* (M4a)
    ├── app_main.c          # entry point: init + tạo task (nối module ở M4d)
    ├── hcsr04.c/.h         # M4b: driver đo khoảng cách HC-SR04
    ├── wifi_mqtt.c/.h      # M4c: Wi-Fi STA, SNTP, MQTT client
    └── telemetry.c/.h      # M4d: payload telemetry_v1, control/ack, app loop
```

## Luồng firmware (app_main)

1. `telemetry_init()` — sinh `boot_id` (16 hex mỗi boot), `sequence = 0`,
   topic telemetry/ack tĩnh, mailbox control (queue depth 4).
2. `hcsr04_init()` — thất bại → log error rồi dừng (thiết bị vô dụng không
   có cảm biến).
3. `hrc_mqtt_on_control(telemetry_handle_control)` — callback chỉ parse tối
   thiểu và đẩy lệnh vào mailbox (không publish trong task esp-mqtt).
4. `hrc_net_init()` — thất bại chỉ log warn rồi tiếp tục: Wi-Fi tự reconnect
   nền, SNTP tự start khi có IP; telemetry vẫn publish với
   `measured_at = null`, `time_synced = false` cho tới khi sync.
5. `hrc_mqtt_start()` — thất bại chỉ log warn; esp-mqtt tự retry.
6. `xTaskCreate(telemetry_task, "telemetry", 4096, ...)` — chu kỳ
   `CONFIG_HRC_SAMPLE_PERIOD_MS` bằng `vTaskDelayUntil`: xả mailbox control
   (ack), rồi đo + build + publish telemetry.
7. `app_main` kết thúc — task app của ESP-IDF tự kết thúc bình thường.

## Edge cases đã quyết định (M4d)

- **Distance > 4500 mm (echo hoàn tất 26.2–30 ms nhưng ngoài miền schema)**:
  mẫu invalid, `error_code = "ECHO_TIMEOUT"`, `distance_mm = null`, và
  `echo_us` **giữ số đo thật** — không clamp số liệu đo được (mức telemetry).
  Khác với ECHO_TIMEOUT **mức driver** (echo KHÔNG hoàn tất: có cạnh lên
  nhưng không có cạnh xuống trước hết timeout, gồm cả xung "không có vật"
  ~38 ms chuẩn HC-SR04) ở đó độ rộng thật không biết (giá trị bị idle
  threshold cắt là clamp, không phải số đo) nên driver luôn trả
  `echo_us = -1` → telemetry publish `echo_us = null`.
- **ECHO_STUCK_HIGH chỉ còn pre-condition**: lỗi này chỉ khi ECHO đang cao
  TRƯỚC khi đo. Echo lên cao rồi không hạ trong timeout được phân loại là
  ECHO_TIMEOUT (`echo_us = null`) — không còn là STUCK_HIGH.
- **Ack khi chưa có mẫu nào** (sequence nội bộ 0): dùng `sequence = 1` vì
  schema yêu cầu minimum 1 ⇒ nếu backend mở phiên ngay khi boot (ack trả
  khi chưa có mẫu nào), ranh giới phiên bắt đầu từ mẫu 2 — mẫu đầu tiên
  không thuộc phiên.
- **measured_at xấp xỉ mốc start đo (µs)**: `utc_ms` được đọc trước khi gọi
  `hcsr04_measure_once()` nên lệch so với mốc `monotonic_us` (esp_timer chốt
  trong driver ngay trước TRIG) một khoảng nhỏ (thiết lập phiên nhận RMT,
  cỡ vài chục µs); timestamp chỉ tới ms nên chấp nhận được.
- **Chưa sync SNTP**: `measured_at = null`, `time_synced = false` — tuyệt
  đối không thay bằng giờ khác (uptime hay giờ backend).
- **Lệnh lạ** ngoài `session_start`/`session_stop`: log warn, KHÔNG ack —
  nhất quán với "thiếu `session_id` → không ack"; không echo chuỗi lạ vào
  JSON ack (vừa vi phạm enum `command` của ack_v1 vừa không escape an toàn).
- **Thiếu `session_id` / payload không parse được `command`**: log warn,
  KHÔNG ack.
- **Mailbox đầy (4 lệnh)**: lệnh mới bị bỏ, log warn.
- **Publish telemetry**: dùng enqueue non-blocking (`hrc_mqtt_enqueue`,
  QoS1, retain=false, store=true) — task đo không bị chặn network timeout
  (~10 s) khi mạng xấu. Khi đứt mạng, esp-mqtt outbox giữ các mẫu và tự
  gửi khi kết nối lại: message nằm trong outbox quá ~30 s
  (`CONFIG_MQTT_OUTBOX_EXPIRED_TIMEOUT_MS`, mặc định 30000 ms) bị xóa,
  và outbox giới hạn ~10 KB (~40 mẫu × ~250 B) — message vượt giới hạn
  bị drop. Enqueue/publish thất bại: chỉ log warn, KHÔNG retry mẫu đó —
  mẫu kế tiếp phát theo chu kỳ. Ack vẫn dùng publish blocking
  (`hrc_mqtt_publish` — hiếm, nhỏ).

## Quy ước payload (tóm tắt)

- Khóa mẫu: `(device_id, boot_id, sequence)` — `sequence` tăng từ 1 trong
  một boot.
- `monotonic_us` = mốc `esp_timer` ngay trước khi driver phát TRIG (mỗi mẫu
  invalid cũng có mốc).
- `distance_mm = echo_us × CONFIG_HRC_SOUND_SPEED_M_S / 2000` (quy đổi số
  nguyên; echo_us đo thời gian âm đi và về nên chia 2).
