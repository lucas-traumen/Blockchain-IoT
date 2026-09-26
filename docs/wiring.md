# Đấu nối và vận hành HRC Safety Log

Hướng dẫn đấu nối phần cứng, cấu hình firmware/broker/backend và thứ tự khởi
động hệ thống. **Phần phần cứng chưa kiểm chứng trên board thật** (firmware
chưa build/flash — xem [iot_code/esp32/README.md](../iot_code/esp32/README.md)).

## 1. Sơ đồ khối và topic MQTT

```
ESP32 + HC-SR04 ──MQTT QoS1──> Mosquitto ──> backend (FastAPI + paho-mqtt)
      ↑ control (backend→device)                  │
      └────────── ack (device→backend) ───────────┘
                                                  ▼
                                        SQLite WAL (data/hrc.db)
                                          ├─ raw_samples
                                          └─ processed_samples (safety + dự báo)
```

Topic (QoS 1, retain=false, `{device_id}` mặc định `esp32_01`):

| Hướng | Topic | Nội dung |
|-------|-------|----------|
| device → backend | `hrc/v1/devices/{device_id}/telemetry` | Payload telemetry_v1 (11 field, mm/µs) |
| backend → device | `hrc/v1/devices/{device_id}/control` | `session_start` / `session_stop` kèm `session_id` |
| device → backend | `hrc/v1/devices/{device_id}/ack` | Xác nhận lệnh control + `boot_id`, `sequence` (ranh giới session) |

Backend subscribe wildcard `hrc/v1/devices/+/telemetry` và
`hrc/v1/devices/+/ack` (xem `backend/ingestion/mqtt_listener.py`).

## 2. Wiring ESP32 + HC-SR04

Theo `configs/sensor.yaml`: `trig_gpio: 4`, `echo_gpio: 5`, `sample_period_ms: 100`,
`echo_timeout_ms: 30`.

| HC-SR04 | ESP32 | Ghi chú |
|---------|-------|---------|
| VCC | 5V (chân 5V/VIN) | HC-SR04 cần 5 V |
| TRIG | GPIO 4 | Output 3.3 V — đủ trigger |
| ECHO | GPIO 5 | **Xung phản hồi 5 V — xem cảnh báo dưới** |
| GND | GND | Bắt buộc dùng chung GND |

> **Cảnh báo an toàn (chưa kiểm chứng phần cứng):** chân ECHO của HC-SR04 xuất
> mức 5 V, trong khi GPIO ESP32 (raw) không chịu 5 V. Nếu board devkit không có
> chuyển mức sẵn, dùng **mạch chia áp** trên ECHO (ví dụ 1 kΩ (từ ECHO) + 2 kΩ
> (xuống GND), lấy điểm giữa ≈ 3.3 V ra GPIO 5). Kiểm tra schematic board cụ thể
> trước khi nối; nối trực tiếp có thể hỏng GPIO.

## 3. Firmware ESP32 (ESP-IDF v5.5)

Build/flash theo hướng dẫn chi tiết trong
[iot_code/esp32/README.md](../iot_code/esp32/README.md) (tóm tắt):

```bash
. $IDF_PATH/export.sh
idf.py set-target esp32
idf.py menuconfig     # đặt các option HRC_* trong menu "HRC Safety Log Firmware"
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

Option Kconfig bắt buộc đặt trong `idf.py menuconfig` (menu *HRC Safety Log
Firmware*):

| Option | Giá trị gợi ý | Ý nghĩa |
|--------|---------------|---------|
| `HRC_WIFI_SSID` | SSID của bạn | Wi-Fi STA |
| `HRC_WIFI_PASS` | mật khẩu | Wi-Fi STA |
| `HRC_MQTT_URI` | `mqtt://<IP-máy-chạy-Mosquitto>:1883` | Trỏ về broker (mặc định `mqtt://192.168.1.10:1883`) |
| `HRC_DEVICE_ID` | `esp32_01` | Khớp `DEVICE_ID` trong `.env` của backend |

Các option còn lại (`HRC_SAMPLE_PERIOD_MS=100`, `HRC_TRIG_GPIO=4`,
`HRC_ECHO_GPIO=5`, `HRC_ECHO_TIMEOUT_MS=30`, `HRC_SOUND_SPEED_M_S=343`) mặc định
đã khớp `configs/sensor.yaml`.

**Trạng thái:** firmware chưa từng qua một lần build thật (không toolchain trên
máy phát triển) — khi build lần đầu cần kiểm tra kết quả theo checklist trong
README firmware.

## 4. Broker MQTT (Mosquitto qua Docker)

Máy phát triển không có mosquitto binary — chạy broker bằng docker
(`eclipse-mosquitto`). Ví dụ (demo nội bộ, không TLS, cho phép client nội bộ
kết nối ẩn danh — chỉ dùng trong mạng thử nghiệm):

```bash
mkdir -p /tmp/opencode/mosquitto && printf 'listener 1883\nallow_anonymous true\n' > /tmp/opencode/mosquitto/mosquitto.conf
docker run -d --name mosquitto -p 1883:1883 \
  -v /tmp/opencode/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf \
  eclipse-mosquitto:2
```

Ghi chú: Mosquitto 2.x mặc định chỉ nhận kết nối local nếu không có config;
file config tối thiểu `listener 1883` + `allow_anonymous true` ở trên để các
client trong mạng LAN (ESP32, backend) kết nối được. Đổi `MQTT_HOST`/`HRC_MQTT_URI`
theo IP máy chạy broker.

## 5. Cấu hình backend (.env)

```bash
cp .env.example .env
```

Sửa tối thiểu trong `.env`:

```
MQTT_HOST=<IP máy chạy Mosquitto>   # mặc định localhost
MQTT_PORT=1883
HRC_MQTT_ENABLED=true               # bật MQTT listener + control transport
HRC_DB_PATH=data/hrc.db
DEVICE_ID=esp32_01                  # khớp HRC_DEVICE_ID của firmware
HRC_ACK_TIMEOUT_S=2.0               # timeout chờ ack lệnh session
```

## 6. Model artifact để live có dự báo

Backend tại thời điểm khởi động tìm `model.joblib` + `manifest.json` trong
`MODEL_ARTIFACT_DIR` (mặc định `ai_model/artifacts/`):

- **Có artifact** → prediction status `OK` với đủ 5 horizon khi đủ cửa sổ.
- **Thiếu artifact** → app vẫn chạy, prediction `MODEL_UNAVAILABLE`.

Train trước khi chạy live:

```bash
.venv/bin/python -m scripts.synthetic_dataset --out data/sim.jsonl --sessions 30 --samples-per-session 200 --seed 42 --scenario all
.venv/bin/python -m ai_model.train --dataset data/sim.jsonl --out-dir ai_model/artifacts
```

Muốn dùng thư mục khác, đặt `MODEL_ARTIFACT_DIR=<đường dẫn>` trong `.env`.

## 7. Thứ tự khởi động chuẩn

1. **Broker:** `docker run ...` (mục 4) — kiểm tra `docker ps` thấy mosquitto.
2. **Backend:** từ repo root, `.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000`.
3. **Thiết bị:** flash firmware (mục 3) rồi cấp nguồn board — firmware tự nối
   Wi-Fi, sync SNTP, publish telemetry chu kỳ 100 ms.
4. **Session:** start/stop bằng CLI hoặc API (xem [data-collection.md](data-collection.md)).

Kiểm tra nhanh sau khi khởi động:

```bash
# mqtt=true nghĩa là backend đã tạo MQTT listener (HRC_MQTT_ENABLED=true)
curl http://localhost:8000/health

# Danh sách session (rỗng ban đầu: [])
curl http://localhost:8000/sessions

# Xem message MQTT thô (terminal thứ hai, tùy chọn — chỉ đọc, không ghi DB)
.venv/bin/python - <<'PY'
import uuid
import paho.mqtt.client as mqtt

def on_message(client, userdata, message):
    print(message.topic, message.payload[:120])

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"watch-{uuid.uuid4().hex[:6]}")
client.on_message = on_message
client.connect("localhost", 1883)
client.subscribe("hrc/v1/devices/#", qos=1)
client.loop_forever()
PY
```

Sau khi start một session và chờ vài giây, kiểm tra mẫu đã vào:

```bash
curl http://localhost:8000/sessions/<session_id>   # stats sample_count > 0
```

Telemetry không hiện từng mẫu trong log backend (chỉ log mức debug); cách xác
minh đáng tin cậy là qua stats session hoặc endpoint trên.

> **Lưu ý:** không chạy `session_cli --mqtt` song song với backend trên cùng
> một DB (xem cảnh báo trong `scripts/session_cli.py`) — một thời điểm chỉ một
> tiến trình ghi DB.
