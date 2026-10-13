# iot_code — Mã nguồn IoT (giả lập và nạp phần cứng)

Mã nguồn IoT cho hệ thống HRC Safety Log: firmware ESP32, backend Python
và simulator.

## Nội dung

```text
iot_code/
├── esp32/              # Firmware ESP-IDF (ESP32/ESP32-S3 + HC-SR04)
│   ├── main/           # app_main, hcsr04, telemetry, wifi_mqtt, lstm_inference
│   ├── CMakeLists.txt
│   ├── sdkconfig.defaults
│   └── dependencies.lock
├── backend/            # Python backend: MQTT, ingestion, safety, inference, session
│   ├── ingestion/      # validate, store (SQLite WAL), MQTT listener
│   ├── safety/         # Công thức Sp, đánh giá CLEAR/VIOLATION/UNKNOWN
│   ├── inference/      # Runtime LSTM
│   ├── sessions/       # Quản lý session START/STOP
│   └── main.py         # FastAPI entrypoint
└── simulator/
    └── device_sim.py   # Giả lập telemetry MQTT
```

Nguồn gốc:
- `esp32/` copy từ `firmware/esp32/` (bỏ `build/`, `managed_components/`,
  `sdkconfig`, `network_config.local.h` chứa Wi-Fi/password riêng).
- `backend/` copy từ `iot_backend/`.
- `simulator/device_sim.py` copy từ `scripts/device_sim.py`.

## Chạy Demo

### 1. Backend + simulator (không cần phần cứng)

```bash
pip install -r ../../requirements.txt
# Mosquitto tại localhost:1883, DB tại data/hrc.db
python -m iot_code.backend.main
python iot_code/simulator/device_sim.py --host localhost --port 1883 --samples 200
```

Lưu ý import: khi chạy từ repo nộp bài, thêm `iot_code/` vào `PYTHONPATH`
hoặc chạy từ thư mục `iot_code/` nếu cần giữ tên package `backend`.

### 2. Nạp phần cứng ESP32

```bash
# Điền Wi-Fi vào esp32/main/network_config.local.h (copy từ network_config.example.h)
idf.py -C iot_code/esp32 set-target esp32s3
idf.py -C iot_code/esp32 build flash monitor
```

Broker MQTT: `mqtt://<IP-MAY-CHAY-DOCKER>:1885`.
Chi tiết đấu nối HC-SR04 (VCC 3,3V, TRIG GPIO4, ECHO GPIO5) xem
`esp32/README.md`.
