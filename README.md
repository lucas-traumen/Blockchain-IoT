# HRC Safety Log — TV3 + TV4

ESP32 + HC-SR04 đo khoảng cách 10 Hz; backend lưu raw, tính ngưỡng SSM `Sp`
và dự báo khoảng cách 100–500 ms bằng LSTM hoặc RF legacy. Assessment và gói
sự kiện được lưu SQLite để TV2 lấy qua API. TV1–TV2 chịu trách nhiệm
canonicalize/hash, Smart Contract và dashboard.

Đơn vị nội bộ SI (m, s, m/s), telemetry dùng mm/µs. Khóa mẫu là
`(device_id, boot_id, sequence)`. Safety giữ `CLEAR`, `VIOLATION`, `UNKNOWN`;
AI chỉ cảnh báo trước vi phạm khi hiện tại còn CLEAR. Dữ liệu synthetic phải
được phân biệt với phép đo thật.

## Kiến trúc

```text
ESP32 / simulator → MQTT QoS1 → validate → raw SQLite → safety + inference
                                                        ├─ processed_samples
                                                        └─ event_packages → API TV2
ai_model: session split → windows/features → train-only scaler → train/evaluate
```

- Topic: `hrc/v1/devices/{device_id}/telemetry|control|ack`, QoS1, retain=false.
- DB: `HRC_DB_PATH` (mặc định `data/hrc.db`), SQLite WAL, DDL tại
  `backend/ingestion/schema.sql`.
- Session khóa bằng boot/sequence từ ack; khi thiếu ack dùng DB fallback.
- LSTM dùng `(N,10,2)` `[distance_m, delta_distance_m]`, `delta[0]=0`, output
  `(N,5)`. RF giữ `d0..d9`; operator chọn model rõ ràng.

## Chạy bằng Docker

MQTT, backend/API và công cụ AI đã có trong [compose.yaml](compose.yaml).
Chạy từ repo root:

```bash
docker compose up -d --build --wait mqtt backend
docker compose ps
curl http://localhost:8001/health
```

Broker dùng cổng host **1885**, API dùng **8001**; bên trong Docker backend
kết nối `mqtt:1883`. SQLite và dữ liệu broker nằm trên named volume, được giữ
khi restart/recreate container. Model mặc định RF; chưa có artifact thì
`model_available=false`, safety vẫn hoạt động. Profile `ai` cung cấp
TensorFlow để train/evaluate hoặc chạy backend LSTM.

Wi-Fi được khai báo bằng macro `HRC_WIFI_NETWORKS` trong
`iot_code/esp32/main/network_config.local.h`: mỗi dòng là `{SSID, mật khẩu}`.
Firmware tạo mảng hai chiều, quét các AP, chọn mạng đã khai báo có RSSI mạnh
nhất và thử mạng tiếp theo nếu nối thất bại. Mất mạng thì quét lại; cấu hình
macro được ưu tiên hoàn toàn trước NVS. File mẫu:
[network_config.example.h](iot_code/esp32/main/network_config.example.h).
Local header không commit; sửa rồi build/flash lại. Broker cũng đặt bằng macro
`HRC_MQTT_URI`, cổng Docker **1885**. Xem [docs/docker.md](docs/docker.md) và
[iot_code/esp32/README.md](iot_code/esp32/README.md) cho cấu hình/build/flash.

## Môi trường Python trực tiếp

Python 3.12, chạy từ repo root:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

RF/backend không cần TensorFlow. Train/evaluate và chạy backend với LSTM dùng
môi trường riêng:

```bash
python3.12 -m venv .venv-ai
.venv-ai/bin/pip install -r requirements-ai.txt
```

`requirements-ai.txt` pin TensorFlow 2.20.0/Keras 3.15.1 và include các dependency
backend. Không cài TensorFlow vào `.venv` baseline. Xem
[docs/lstm.md](docs/lstm.md) để chọn artifact và runtime.

## Lệnh chính

```bash
# Backend + RF regression; test TensorFlow skip ở môi trường này
.venv/bin/python -m pytest tests/ -q

# Toàn bộ test, gồm artifact LSTM thật
TF_NUM_INTEROP_THREADS=1 TF_NUM_INTRAOP_THREADS=1 OMP_NUM_THREADS=1 \
  .venv-ai/bin/python -m pytest tests/ -q

# Sinh dữ liệu synthetic cho smoke, giữ nhãn nguồn
.venv/bin/python -m scripts.synthetic_dataset --out data/sim.jsonl \
  --sessions 30 --samples-per-session 200 --seed 42 --scenario all

# Train và đánh giá LSTM offline; test dùng đúng split/hash artifact
.venv-ai/bin/python -m ai_model.lstm_train --dataset data/sim.jsonl \
  --out-dir ai_model/artifacts/lstm-v1
.venv-ai/bin/python -m ai_model.evaluate --model-type lstm \
  --dataset data/sim.jsonl --model-dir ai_model/artifacts/lstm-v1 \
  --output data/lstm-report.json

# Backend LSTM, cần broker tại MQTT_HOST/MQTT_PORT
MODEL_TYPE=lstm MODEL_ARTIFACT_DIR=ai_model/artifacts/lstm-v1 \
  .venv-ai/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000

# RF legacy/rollback
.venv/bin/python -m ai_model.train --dataset data/sim.jsonl --out-dir ai_model/artifacts
MODEL_TYPE=rf .venv/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000

# MQTT simulator, telemetry synthetic; recording giữ nhãn nguồn
.venv/bin/python -m scripts.device_sim --host localhost --port 1883 \
  --samples 200 --scenario approach --record-jsonl data/simulator.jsonl

# Replay LSTM offline, version mới để giữ nguyên lịch sử live
.venv-ai/bin/python -m scripts.replay --db data/hrc.db --model-type lstm \
  --model-dir ai_model/artifacts/lstm-v1 --processor-version replay-lstm-v1

# Export dataset có session metadata; nguồn phải khai tường minh
.venv/bin/python -m scripts.export_dataset --db data/hrc.db \
  --out data/measured-sessions.jsonl --source real

# Báo cáo hiệu chuẩn từ CSV đo được; không tự sửa configs/safety.yaml
.venv/bin/python -m scripts.calibrate --input data/calibration.csv \
  --output data/calibration-report.json
```

`MODEL_TYPE=rf` là mặc định để giữ hành vi baseline. Khi chọn LSTM mà thiếu
TensorFlow/artifact hoặc model lỗi, prediction trả `MODEL_UNAVAILABLE`; safety
vẫn tính. Không tự chuyển model âm thầm.

## API

| Method | Route | Dữ liệu |
|---|---|---|
| GET | `/health` | MQTT, DB, processor version, model type/availability |
| POST | `/sessions/start` | Mở session: `scenario`, `object`, `zone_note`, `session_id` |
| POST | `/sessions/{session_id}/stop` | Đóng session |
| GET | `/sessions`, `/sessions/{session_id}` | Session và thống kê |
| GET | `/sessions/{session_id}/export?format=jsonl\|csv` | Raw telemetry |
| GET | `/assessments?session_id=&processor_version=&limit=` | Assessment theo version |
| GET | `/samples/{device_id}/{boot_id}/{sequence}` | Tra assessment |
| GET | `/events?session_id=&processor_version=&limit=` | Trigger + assessment đầu tiên |
| GET | `/events/{event_id}` | Gói raw 2s trước/1s sau và coverage |

`PREDICTED_WARNING` cần 2/3 mẫu cảnh báo, không dùng dự báo stale. `VIOLATION`
và `UNKNOWN` phát sự kiện khi chuyển trạng thái. Gói thiếu raw báo coverage
chưa đủ; không thêm mẫu giả. Xem [docs/tv2_handoff.md](docs/tv2_handoff.md).

## Kiểm chứng và giới hạn

Trạng thái/test cuối cùng ghi trong [PLAN.md](PLAN.md). Phiên 02/10/2026 thực
hiện code, review và test trực tiếp, không dùng subagent. Smoke LSTM là kiểm
tra pipeline trên synthetic; chưa chứng minh chất lượng dự báo trên dữ liệu
HC-SR04 thật.

Firmware đã **build PASS trên ESP-IDF 6.0.1 cho ESP32-S3 và
ESP32** ngày 02/10/2026. Trong phiên đã nhận diện **ESP32-D0WD-V3 revision
v3.1** ở `/dev/ttyUSB0` và **ESP32-S3 revision v0.2** ở `/dev/ttyACM0`.
Cổng có thể đổi; chọn đúng chip/target và cổng khi build/flash. Người dùng đã cung cấp log HRC
chạy thật; bản sửa mới và khả năng đo/mạng trên bo vẫn chưa kiểm chứng.
Điện áp Echo, nhiễu, hiệu chuẩn và soak là các gate phần cứng còn mở.
Tham số robot demo không phải chứng nhận ISO. Xem
[iot_code/esp32/README.md](iot_code/esp32/README.md).

## Tài liệu

- [docs/docker.md](docs/docker.md): MQTT/backend/AI, cổng và dữ liệu Docker.
- [docs/lstm.md](docs/lstm.md): môi trường, train/evaluate, chọn model, runtime.
- [docs/wiring.md](docs/wiring.md): đấu nối, firmware và broker.
- [docs/data-collection.md](docs/data-collection.md): thu/export dữ liệu theo session.
- [docs/calibration.md](docs/calibration.md): định dạng và công thức báo cáo hiệu chuẩn.
- [docs/tv2_handoff.md](docs/tv2_handoff.md): assessment và event package.
- [docs/experiments.md](docs/experiments.md): các smoke/thí nghiệm tái lập.
