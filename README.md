# HRC Safety Log — TV3 + TV4

Demo hộp đen an toàn Human–Robot Collaboration (HRC): ESP32 + HC-SR04 đo khoảng
cách 10 Hz, backend đánh giá ngưỡng an toàn SSM (Sp) theo cấu hình phiên bản hóa,
đồng thời dự báo khoảng cách 5 horizon (100–500 ms) bằng RandomForest, ghi vết
mọi mẫu kèm kết quả đánh giá (assessment_v1) để bàn giao TV2.

Phạm vi repo: **TV3** (ESP32, thu dữ liệu, thuật toán ngưỡng) + **TV4** (dataset,
AI dự báo, đánh giá). TV1–TV2 (Smart Contract, hash, dashboard, bộ gom sự kiện)
**ngoài phạm vi** — TV2 chỉ nhận dữ liệu qua assessment API/export.

Quy ước chính: đơn vị nội bộ SI (m, s, m/s), payload telemetry giữ mm + µs
(chuyển đổi tại biên); khóa mẫu `(device_id, boot_id, sequence)`; không tự giảm
Sp, không biến UNKNOWN thành CLEAR; dữ liệu synthetic luôn ghi nhãn `synthetic`.

## Kiến trúc

```
ESP32 + HC-SR04 ──MQTT QoS1──> Mosquitto ──> backend/ingestion (validate → lưu raw SQLite → compute)
                                               ├─ backend/safety   (Sp, CLEAR/VIOLATION/UNKNOWN)
                                               ├─ backend/inference (window 10 mẫu → RF dự báo 5 horizon)
                                               └─ processed_samples (bàn giao TV2: API/export, outbox)
ai_model/  : windows/features dùng chung train+runtime, prepare/split, baselines, train RF, evaluate
scripts/   : session CLI, export, replay (offline), ingest JSONL, synthetic dataset
```

- Topic MQTT: `hrc/v1/devices/{device_id}/telemetry|control|ack`, QoS 1,
  retain=false.
- DB: SQLite WAL tại `HRC_DB_PATH` (mặc định `data/hrc.db`); DDL trong
  `backend/ingestion/schema.sql`.
- Ranh giới session lấy từ ack thiết bị (fallback: sequence lớn nhất đã nhận —
  `boundary_source = device_ack|db_fallback`).

## API chính (`backend/main.py`)

| Method | Route | Ý nghĩa |
|--------|-------|---------|
| GET | `/health` | Trạng thái app (mqtt bật/không, db, processor_version) |
| POST | `/sessions/start` | Bắt đầu session (body: `scenario`, `object`, `zone_note`, `session_id` — tùy chọn) |
| POST | `/sessions/{session_id}/stop` | Đóng session |
| GET | `/sessions` | Danh sách session |
| GET | `/sessions/{session_id}` | Chi tiết session + stats |
| GET | `/sessions/{session_id}/export?format=jsonl\|csv` | Export raw samples của session |
| GET | `/assessments?session_id=&processor_version=&limit=` | Assessment (result_json) theo session hoặc toàn bộ |
| GET | `/samples/{device_id}/{boot_id}/{sequence}` | Tra một assessment theo khóa mẫu |

## Cài đặt

Cần Python 3.12 (đã thử với 3.12.3), chạy từ repo root, không cài package:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # chỉnh MQTT_HOST/MQTT_PORT... theo môi trường
```

Biến môi trường (xem `.env.example`): `MQTT_HOST`, `MQTT_PORT`, `HRC_DB_PATH`,
`DEVICE_ID`, `SAFETY_CONFIG`, `MODEL_CONFIG`, `MODEL_ARTIFACT_DIR`,
`HRC_MQTT_ENABLED`, `HRC_ACK_TIMEOUT_S`.

## Lệnh chuẩn (chạy từ repo root)

```bash
# Chạy toàn bộ test (147 test)
.venv/bin/python -m pytest tests/ -q

# Khởi động backend (MQTT_HOST... từ .env hoặc môi trường)
.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000

# Sinh dataset synthetic (JSONL, ghi nhãn synthetic/scenario/session_label)
.venv/bin/python -m scripts.synthetic_dataset --out data/sim.jsonl \
    --sessions 30 --samples-per-session 200 --seed 42 --scenario all

# Train RandomForest v1 → ai_model/artifacts/ (model.joblib + manifest.json)
.venv/bin/python -m ai_model.train --dataset data/sim.jsonl --out-dir ai_model/artifacts

# Đánh giá RF + baselines (persistence, constant-velocity) trên split test
.venv/bin/python -m ai_model.evaluate --dataset data/sim.jsonl --model-dir ai_model/artifacts

# Nạp telemetry JSONL vào DB (mẫu synthetic phải tách 3 nhãn metadata trước — xem docs/data-collection.md)
.venv/bin/python -m scripts.ingest_jsonl --input <file.jsonl>

# Quản lý lượt thu: start / stop / list / status / export
.venv/bin/python -m scripts.session_cli start --scenario demo
.venv/bin/python -m scripts.session_cli stop --session-id <session_id>
.venv/bin/python -m scripts.session_cli list
.venv/bin/python -m scripts.session_cli status --session-id <session_id>
.venv/bin/python -m scripts.session_cli export --session-id <session_id> --format csv --out out.csv

# Replay offline: tính lại safety cho raw đã lưu (processor_version riêng, idempotent)
.venv/bin/python -m scripts.replay --db data/hrc.db --session-id <session_id>
```

## Cấu trúc repo

| Thư mục | Nội dung |
|---------|----------|
| `backend/` | FastAPI app, ingestion (validate/store/MQTT), safety (SSM), inference (RF runtime), sessions (control/ack, export) |
| `ai_model/` | windows/features dùng chung train+runtime, prepare/split, baselines, train, evaluate, events |
| `scripts/` | `session_cli.py`, `ingest_jsonl.py`, `replay.py`, `synthetic_dataset.py` |
| `schemas/` | JSON Schema v1: telemetry, assessment, control, ack (Draft 2020-12) |
| `configs/` | `safety.yaml` (Sp theo `valid_from`), `model.yaml` (window/horizons), `sensor.yaml` (HC-SR04) |
| `iot_code/` | Firmware ESP32 (ESP-IDF v5.5) — xem `iot_code/esp32/README.md` |
| `docs/` | Hướng dẫn đấu nối/vận hành, quy trình thu dữ liệu, bàn giao TV2, thí nghiệm |
| `tests/` | pytest (không cần broker thật) |

## Trạng thái kiểm chứng

- Backend + ai_model: **147 test pass** (`.venv/bin/python -m pytest tests/ -q`).
- Firmware ESP32: code hoàn chỉnh nhưng **chưa build/flash** (không có toolchain
  ESP-IDF trên máy phát triển) — phần chưa kiểm chứng liệt kê trong
  [iot_code/esp32/README.md](iot_code/esp32/README.md).
- Dữ liệu demo là **synthetic có nhãn** (`"synthetic": true`), không phải đo thật.

## Tài liệu

- [docs/wiring.md](docs/wiring.md) — đấu nối phần cứng, firmware, broker, thứ tự khởi động.
- [docs/data-collection.md](docs/data-collection.md) — quy trình thu dữ liệu (thật, synthetic, ingest, replay, export).
- [docs/tv2_handoff.md](docs/tv2_handoff.md) — bàn giao dữ liệu cho TV2.
- [docs/experiments.md](docs/experiments.md) — kết quả thí nghiệm.
