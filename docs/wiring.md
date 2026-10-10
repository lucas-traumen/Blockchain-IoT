# Đấu nối và vận hành HRC Safety Log

Firmware đã build trên **ESP-IDF 6.0.1 cho ESP32 và ESP32-S3**.
Bản sửa mới và phần điện/đo trên bo chưa kiểm chứng; xem
[firmware/esp32/README.md](../firmware/esp32/README.md). Host MQTT, iot_backend và
công cụ AI chạy bằng [Docker Compose](docker.md).

## Sơ đồ khối và topic

```text
ESP32 + HC-SR04 → MQTT QoS1 → Docker Mosquitto → Docker iot_backend
       ↑ control/ack                              ├─ raw SQLite
                                                  ├─ safety + inference
                                                  └─ assessment/event API
```

| Hướng | Topic | Dữ liệu |
|---|---|---|
| device → iot_backend | `hrc/v1/devices/{device_id}/telemetry` | 11 field telemetry_v1, mm/µs |
| iot_backend → device | `hrc/v1/devices/{device_id}/control` | session_start/session_stop, session_id |
| device → iot_backend | `hrc/v1/devices/{device_id}/ack` | accepted, boot_id, sequence |

QoS1, retain=false. `device_id` mặc định `esp32_01`, phải khớp
`HRC_DEVICE_ID` trong Compose để lệnh session được gửi đến đúng thiết bị.
Backend subscribe wildcard telemetry/ack của các device.

## ESP32 + HC-SR04

Theo `configs/sensor.yaml`: TRIG GPIO4, ECHO GPIO5, chu kỳ 100 ms, timeout30 ms.

| HC-SR04 | ESP32 | Ghi chú |
|---|---|---|
| VCC | 5V/VIN | Theo nguồn/schematic bo |
| TRIG | GPIO4 | Output trigger 10 µs |
| ECHO | GPIO5 qua chuyển mức | Không nối Echo 5 V trực tiếp vào GPIO |
| GND | GND | Nguồn và bo phải có GND chung |

HC-SR04 Echo mức 5 V cần mạch chia áp/chuyển mức. Ví dụ: ECHO → 1 kΩ →
điểm giữa → 2 kΩ → GND; điểm giữa khoảng 3.3 V nối GPIO5. Kiểm tra schematic
và điện áp thực tế. Chọn chân theo **số GPIO**, không theo vị trí vật lý hoặc
nhãn D4/D5 của bo khác. Nếu firmware luôn báo ECHO_STUCK_LOW, kiểm tra đường
TRIG/ECHO, nguồn và GND; đổi mức log không tạo ra xung phản hồi.

## Khởi động host bằng Docker

Từ repo root:

```bash
docker compose up -d --build --wait mqtt iot_backend
docker compose ps
curl http://localhost:8001/health
```

Broker **1885** trên host →1883 trong Docker, API **8001** →8000.
Backend kết nối `mqtt:1883`; ESP32 kết nối `mqtt://<IP-LAN-host>:1885`.
Ngày 2026-10-02 IP host là `192.168.100.3`. Dùng IP hiện tại khi mạng đổi.
Không dùng localhost trên ESP32 hoặc broker của dự án khác.

SQLite/broker có named volume. Model chưa có artifact thì prediction trả
MODEL_UNAVAILABLE; safety vẫn hoạt động. Chọn runtime LSTM và AI theo
[docker.md](docker.md), [lstm.md](lstm.md).

## Khai báo Wi-Fi rồi build/flash đúng chip

Khai báo Wi-Fi **trước khi build** trong
`firmware/esp32/main/network_config.local.h`:

```c
#define HRC_WIFI_LIST_MODE 1
#define HRC_WIFI_NETWORKS { \
    {"WiFi_cua_ban", "mat_khau_wifi"}, \
    {"Hotspot", "mat_khau_hotspot"}, \
}
#define HRC_MQTT_URI "mqtt://192.168.100.3:1885"
```

File đã có trong workspace; clone mới copy từ `network_config.example.h`.
Mỗi dòng trong mảng hai chiều là SSID/password. Firmware quét, chọn mạng
mạnh nhất đã khai báo, thử mạng tiếp theo nếu thất bại và quét lại khi mất
kết nối. Không cần gõ Wi-Fi trong monitor. NVS cũ không ghi đè macro.
Sửa danh sách/broker cần build/flash lại; local header không commit.
Serial/NVS chỉ hoạt động ở chế độ cũ `HRC_WIFI_LIST_MODE=0`.

Trong phiên ngày 2026-10-02 đã nhận diện ESP32-D0WD-V3 revision v3.1 ở
`/dev/ttyUSB0` và ESP32-S3 revision v0.2 ở `/dev/ttyACM0`. Cổng có thể đổi
khi cắm lại; dùng chip/cổng thực tế để chọn target. Ví dụ cho ESP32 trong
ESP-IDF Terminal:

```bash
cd firmware/esp32
HRC_PORT=/dev/ttyUSB0   # thay bằng cổng thực tế của bo
python -m esptool --port "$HRC_PORT" chip-id
idf.py set-target esp32
idf.py menuconfig      # kiểm tra GPIO và device_id
idf.py build
idf.py -p "$HRC_PORT" flash monitor
```

ESP32-S3 dùng `set-target esp32s3` và port tương ứng; binary hai target
không dùng chung. `set-target` tạo lại sdkconfig nên làm trước menuconfig.
Nếu target đã đúng, chỉ cần build/flash. VS Code flash type UART.

## Kiểm tra telemetry và session

Sau flash, monitor in `distance_mm`, `echo_us`, `valid` và `sequence` mỗi
1 s. Đổi `HRC_DISTANCE_LOG_PERIOD_MS=100` qua menuconfig để xem từng mẫu.
Chờ log `control ready (QoS1, ...)` để xác nhận subscribe control đã được
broker chấp nhận. Task gửi riêng và bộ đếm PUBACK giúp theo dõi MQTT; xem
[firmware README](../firmware/esp32/README.md#log-khoảng-cách-và-gửi-mqtt).


```bash
# Chỉ xem telemetry, không ghi DB
docker compose exec mqtt mosquitto_sub -h localhost \
  -t 'hrc/v1/devices/esp32_01/telemetry' -C 3 -W 15

curl -X POST http://localhost:8001/sessions/start \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"run_001","scenario":"approach","object":"hop"}'
curl http://localhost:8001/sessions/run_001
curl -X POST http://localhost:8001/sessions/run_001/stop
```

`sample_count>0` xác nhận ingest; `boundary_source=device_ack` xác nhận cả
control/ack. `mqtt=true` trên health chỉ báo listener bật. Mẫu sensor invalid
vẫn lưu raw nhưng safety UNKNOWN. Không chạy session_cli --mqtt song song
để ghi cùng DB iot_backend. Thu/export dữ liệu theo [data-collection.md](data-collection.md).
