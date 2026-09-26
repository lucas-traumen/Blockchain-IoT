# PLAN — HRC Safety Log: TV3 + TV4

Triển khai mã chạy được cho TV3 (ESP32, thu dữ liệu, thuật toán ngưỡng) và TV4 (dataset, AI dự báo, đánh giá) theo spec điều hành. Repo khởi đầu trống — xây mới toàn bộ theo cấu trúc dưới đây. TV1–TV2 (Smart Contract, hash, dashboard, bộ gom sự kiện) nằm ngoài phạm vi.

## 1. Kiến trúc

```
ESP32 + HC-SR04 ──MQTT QoS1──> Mosquitto ──> backend/ingestion (validate → lưu raw SQLite → compute)
                                               ├─ backend/safety   (Sp, CLEAR/VIOLATION/UNKNOWN)
                                               ├─ backend/inference (window 10 mẫu → RF dự báo 5 horizon)
                                               └─ processed_samples (bàn giao TV2: API/export, outbox)
ai_model/  : windows/features dùng chung train+runtime, prepare/split, baselines, train RF, evaluate, predict
scripts/   : session CLI, export, replay (offline), ingest JSONL, device simulator, synthetic dataset
```

Đơn vị nội bộ: **SI (m, s, m/s)**. Payload telemetry giữ mm + µs như spec. Chuyển đổi tại biên: `distance_m = distance_mm / 1000`.

## 2. Quy ước chung (bắt buộc mọi milestone)

- Python: venv `.venv/` (đã tạo, Python 3.12.3). Chạy từ repo root: `.venv/bin/python -m pytest tests/ -q`, `.venv/bin/python -m scripts.session_cli ...`, `.venv/bin/uvicorn backend.main:app`.
- Import: package top-level `backend`, `ai_model`, `scripts` (có `__init__.py`); không cài đặt, chạy từ repo root.
- Cấu hình qua env + `.env.example` (không bí mật): `MQTT_HOST=localhost`, `MQTT_PORT=1883`, `HRC_DB_PATH=data/hrc.db`, `DEVICE_ID=esp32_01`, `SAFETY_CONFIG=configs/safety.yaml`, `MODEL_CONFIG=configs/model.yaml`, `MODEL_ARTIFACT_DIR=ai_model/artifacts`. Nạp bằng `python-dotenv`.
- Topic: telemetry `hrc/v1/devices/{device_id}/telemetry` (QoS1, retain=false); control `hrc/v1/devices/{device_id}/control` (backend→device); ack `hrc/v1/devices/{device_id}/ack` (device→backend).
- Khóa mẫu: `(device_id, boot_id, sequence)`. Không dùng thứ tự nhận MQTT làm thứ tự đo.
- Schema JSON: `schemas/telemetry_v1.schema.json`, `schemas/assessment_v1.schema.json`, `schemas/control_v1.schema.json`, `schemas/ack_v1.schema.json`. Validation bằng `jsonschema` (Draft 2020-12).
- DB: SQLite WAL tại `HRC_DB_PATH`; DDL duy nhất trong `backend/ingestion/schema.sql`; mọi truy cập qua `backend/ingestion/store.py`.
- Dữ liệu/artifact không commit: `data/` (trừ `data/samples/` mẫu nhỏ), `ai_model/artifacts/`, `.venv/`, `sdkconfig`, `managed_components/`.
- Không tự giảm Sp; không biến UNKNOWN thành CLEAR; không bịa kết quả thực nghiệm — dữ liệu synthetic phải ghi nhãn `synthetic` rõ ràng.

### Bảng DB (DDL chuẩn trong `backend/ingestion/schema.sql`)

- `raw_samples(id, device_id, boot_id, sequence, received_at, is_late, payload_json, measured_at, monotonic_us, time_synced, distance_mm, echo_us, valid, error_code)` — UNIQUE(device_id, boot_id, sequence).
- `rejected_messages(id, received_at, topic, reason, payload_text)`.
- `sessions(session_id PK, device_id, scenario, status, object, zone_note, started_at, ended_at, config_json, start_boot_id, start_sequence, stop_sequence, boundary_source, boot_change)` — ranh giới session lấy từ ack thiết bị (fallback: sequence lớn nhất đã nhận của boot hiện tại tại thời điểm start); `boundary_source` = `device_ack|db_fallback`.
- `session_samples(session_id, device_id, boot_id, sequence, PRIMARY KEY(session_id, device_id, boot_id, sequence))` — gán theo khoảng sequence `start_sequence <= seq` (cùng boot `start_boot_id`) và `seq <= stop_sequence` nếu đã stop (khoảng đóng `[start_sequence, stop_sequence]`; `start_sequence = ack.sequence + 1`, `stop_sequence = ack.sequence` — mẫu tại lệnh stop được đo trong session); boot khác → không gán + đánh dấu `sessions.boot_change=1`.
- `processed_samples(id, device_id, boot_id, sequence, processor_version, computed_at, mode, result_json)` — UNIQUE(device_id, boot_id, sequence, processor_version). `mode` = `live|offline`. `result_json` = bản ghi bàn giao TV2 (schema assessment_v1).

### Payload telemetry v1 (giữ nguyên theo spec điều hành)

```json
{
  "schema_version": "1.0", "device_id": "esp32_01", "boot_id": "unique-per-boot",
  "sequence": 125, "monotonic_us": 12500000, "measured_at": "2026-09-21T12:00:12.500Z",
  "time_synced": true, "distance_mm": 643, "echo_us": 3746, "valid": true, "error_code": null
}
```

Quy tắc: `measured_at` tại lúc bắt đầu đo; chưa sync → `measured_at=null, time_synced=false` (không thay bằng giờ backend). Timeout → `distance_mm=null, valid=false, error_code` thuộc enum `ECHO_TIMEOUT|ECHO_STUCK_LOW|ECHO_STUCK_HIGH|TRIG_FAIL`; `echo_us` giữ nếu đo được. Miền giá trị: `sequence>=1`, `monotonic_us>=0`, `distance_mm` 0..4500, `echo_us` 1..30000. Mẫu `valid=false` đúng schema vẫn lưu raw.

### Bản ghi bàn giao TV2 (assessment_v1) — đúng hình spec §8

```json
{
  "schema_version": "1.0",
  "sample_ref": {"device_id": "esp32_01", "boot_id": "b1", "sequence": 125},
  "session_id": "run_001", "measured_at": "...", "computed_at": "...", "received_at": "...",
  "distance_m": 0.643,
  "safety": {"status": "CLEAR", "sensor_valid": true, "assessment_valid": true,
             "sp_m": 0.44, "margin_m": 0.203, "config_version": "demo-v1",
             "config_time_source": "measured_at",
             "components": {"sh_m": 0.30, "sr_m": 0.02, "ss_m": 0.02, "c_m": 0.05, "zd_m": 0.03, "zr_m": 0.02},
             "config_fit_warnings": []},
  "prediction": {"status": "OK", "model_version": "rf-v1",
                 "horizons_ms": [100, 200, 300, 400, 500], "distance_m": [0.60, 0.56, 0.51, 0.47, 0.42],
                 "early_warning": true, "stale": false, "window_ref": {...},
                 "mode": "live"}
}
```

- `prediction.status` ∈ `OK|WARMUP|INVALID_WINDOW|MODEL_UNAVAILABLE`; `distance_m`/`early_warning` null khi không OK.
- `stale`: live mode, `computed_at - measured_at > configs/model.yaml: stale_after_s` → true. Replay: `mode=offline`, tính theo thời gian nguồn.
- `window_ref`: `{device_id, boot_id, sequence_first, sequence_last, sample_count, max_age_ms}` của 10 mẫu đầu vào.
- `config_time_source`: `measured_at` khi đã sync; fallback `received_at` khi `measured_at=null` (ghi chú xấp xỉ trong docs).

## 3. Thuật toán ngưỡng (backend/safety)

Hàm thuần, SI: `Sh = vh*(Tr+Ts)`, `Sr = vr*Tr`, `Ss = vr*Ts/2`, `Sp = Sh+Sr+Ss+C+Zd+Zr`, `margin_m = distance_m - Sp`.

- `configs/safety.yaml`: danh sách phiên bản `config_version, valid_from (ISO-8601 UTC), params{vh, vr, tr_s, ts_s, c_m, zd_m, zr_m}, measurement_zone_m{min: 0.10, max: 1.00}`. Chọn config cho thời điểm `t`: phiên bản có `valid_from <= t` lớn nhất (mới nhất trong quá khứ, không phải mới nhất tuyệt đối).
- Trạng thái: mẫu hợp lệ + `margin_m > 0` → `CLEAR`; `margin_m <= 0` → `VIOLATION`; mẫu lỗi/thiếu → `UNKNOWN` (tách `sensor_valid` và `assessment_valid`).
- Cảnh báo cấu hình (không chặn, không giảm Sp): `sp_m <= measurement_zone_m.min` (không còn vùng quan sát trước ngưỡng) hoặc `sp_m > measurement_zone_m.max` (Sp ngoài vùng đã kiểm chứng) → thêm chuỗi vào `config_fit_warnings`.
- Fixture đối chiếu: vh=1, vr=0.2, Tr=0.1, Ts=0.2, C=0.05, Zd=0.03, Zr=0.02 → Sp = 0.44 m chính xác.

## 4. Dataset, model, inference (ai_model + backend/inference)

- Cửa sổ kết thúc tại t: `X = [d(t-9Δ), ..., d(t)]`, `y = [d(t+Δ), ..., d(t+5Δ)]`, Δ=0.1s. Horizons `[100,200,300,400,500]` ms tính từ `measured_at` mẫu cuối.
- Windowing/feature **dùng chung train + runtime**: `ai_model/windows.py` (extract windows từ DataFrame/rows: cùng session+boot, sequence liên tiếp, không mẫu lỗi, gap thời gian ≤ `max_gap_s`, jitter nhịp ≤ `max_time_jitter_s`) và `ai_model/features.py` (feature vector thứ tự chuẩn). Không tạo cửa sổ qua session/boot; drop có thống kê (lý do: gap/error/short).
- Model v1: `RandomForestRegressor` multi-output (5 output). Feature: 10 khoảng cách (m). Không dùng sequence/session_id/scenario/kết quả tương lai làm feature. Baseline cùng split: persistence (d(t) lặp) và constant-velocity (v ước lượng từ cửa sổ quá khứ, ngoại suy `d(t)+v*h`).
- Split theo session 70/15/15, seed ghi trong split manifest; chia theo buổi nếu các lượt cùng buổi gần giống. Chọn hyperparameter bằng validation; test chỉ dùng để báo cáo.
- Artifact `ai_model/artifacts/`: model.joblib + `manifest.json` (feature_names, units, window_size, delta_s, horizons_ms, preprocessing_version, model_version, seed, sklearn/pandas/numpy versions, split manifest hash, created_at). Chỉ tải từ đường dẫn cấu hình tin cậy.
- Inference runtime (backend/inference): buffer per (device_id, boot_id, session_id); <10 mẫu → WARMUP; mẫu lỗi/gap → INVALID_WINDOW; thiếu model → MODEL_UNAVAILABLE; đủ 10 → dự báo 5 horizon. Early warning: hiện tại CLEAR và ≥1 dự báo ≤ Sp → true; hiện tại VIOLATION → giữ nhãn VIOLATION.
- Đánh giá: MAE theo từng horizon cho 3 mô hình; sự kiện = lần khoảng cách thực chạm/crossing ≤ Sp (từ trên xuống) trong test; gộp cảnh báo liên tiếp trước khi đếm; một cảnh báo đúng iff có lần chạm trong chân trời dự báo đã chọn (không ghép tùy ý với sự kiện xa hơn); báo precision/recall, cảnh báo sai mỗi phút, lead time; không có sự kiện → ghi `null` (không xác định), không bịa.

## 5. Milestones

| ID | Nội dung | Files chính | Kiểm tra |
|----|----------|-------------|----------|
| M1 | Scaffold + schemas + configs + module safety + test safety | `requirements.txt`, `.env.example`, `configs/*`, `schemas/*`, `backend/safety/*`, `tests/test_safety.py` | `pytest tests/test_safety.py -q` |
| M2 | Ingestion core: validate, store, dedup, gap/late, catch-up, ingest_jsonl + test | `backend/ingestion/*`, `scripts/ingest_jsonl.py`, `tests/test_ingestion.py` | `pytest tests/test_ingestion.py -q` |
| M3 | Sessions (control MQTT + fallback), CLI, FastAPI, replay + test | `backend/sessions/*`, `backend/main.py`, `scripts/session_cli.py`, `scripts/replay.py`, `tests/test_sessions.py`, `tests/test_api.py` | `pytest tests/test_sessions.py tests/test_api.py -q` |
| M4 | Firmware ESP-IDF hoàn chỉnh | `iot_code/esp32/**` | Review thủ công (không có IDF/toolchain tại máy này) |
| M5 | ai_model: windows/features, synthetic, split, baselines, train, evaluate + test | `ai_model/*`, `scripts/synthetic_dataset.py`, `tests/test_windows.py`, `tests/test_model.py`, `tests/test_events.py` | `pytest tests/test_windows.py tests/test_model.py tests/test_events.py -q` |
| M6 | backend/inference + tích hợp compute + bàn giao TV2 + stale + test tích hợp | `backend/inference/*`, sửa `backend/ingestion/service.py`, `tests/test_inference.py`, `tests/test_integration.py` | `pytest tests/test_inference.py tests/test_integration.py -q` |
| M7 | README, docs (wiring, thu dữ liệu, tv2_handoff, experiments), dọn dẹp | `README.md`, `docs/*`, `scripts/device_sim.py` | `pytest tests/ -q` toàn bộ + soát lệnh README khớp code |

Tiêu chí nghiệm thu chi tiết: bảng ở spec điều hành §9 — mọi dòng phải có test tương ứng hoặc ghi rõ chưa kiểm chứng được (firmware trên phần cứng).

## 6. Trạng thái

| Milestone | Trạng thái | Ghi chú |
|-----------|-----------|---------|
| M1 | done | 18/18 test pass; review độc lập gộp vào M1M2M3:reviewer (đã hoàn tất) |
| M2 | done | 34/34 test; orchestrator đọc 100% file M2, khớp spec |
| M3 | done | 55/55 test sau fix M3b (ingest_jsonl gán session); smoke test end-to-end của orchestrator pass |

Review độc lập M1–M3 (M1M2M3:reviewer): PASS WITH NOTES — 0 BLOCKER, 1 MAJOR (M-1 double-stop), 7 MINOR, nhiều INFO. Orchestrator spot-check 6/9 finding chính bằng đọc trực tiếp code — tất cả khớp. Fix gom vào task M3c (bảng trên); biên session `seq <= stop_sequence` đã cập nhật PLAN mục 2 cho khớp chủ ý code (m-1).
| M4 | done (code) | M4a–M4d + M4e: 12 file ESP-IDF v5.5, review độc lập xong (1 BLOCKER esp-mqtt API + 2 MAJOR RMT — đã fix hết trong M4e, verified). Payload 5/5 PASS jsonschema. CHƯA build/flash được (không toolchain) — README firmware liệt kê đủ phần chưa kiểm chứng; khi có ESP-IDF v5.5: build + test board case F2/F3 (reset receiver, phân loại TIMEOUT) |
| M5 | done | M5a windows/features (24 test) + M5b synthetic dataset/split (12) + M5c train RF + baselines + artifact (10) + M5d1 events logic (22) + M5d2 evaluate CLI (1) — 134 passed tổng. Kết quả thật trên dataset synthetic smoke (12×80): RF MAE [0.017..0.084]m thắng persistence, thua constant-velocity [0.013..0.072]m; event metrics precision 1.0 / recall 0.5 / lead 0.1s. M5d nguyên khối rỗng 1 lần → tách M5d1/M5d2 chạy ngay (bài học flash đúng lần 2) |
| M6 | done | M6a1 predictor (backend/inference/predictor.py) + M6a2 test (10) + M6b tích hợp _compute_and_store + main.py + replay/ingest_jsonl --model-dir + test_integration (3) — 147 passed. Orchestrator tự chạy E2E độc lập: WARMUP/OK/VIOLATION+early_warning/replay offline idempotent đều đúng. sp None → inf (early_warning False, không bịa). Predictor CLI là instance riêng từng lần chạy |
| M7 | pending | |
| M3c | done | Fix đủ 8 finding review M1–M3 (double-stop 409, latest_raw theo boot mới, pin NULL-boot + boot_change, .env, catch_up CLI/mqtt_listener, PK conflict 409, .env.example, session_stats bỏ hardcode). 65 passed (55+10 test mới) |

Phiên bản dependency đã cài trong `.venv` (pin vào requirements.txt ở M1): paho-mqtt==2.1.0, fastapi==0.141.1, uvicorn==0.53.0, pydantic==2.13.5, jsonschema==4.26.0, PyYAML==6.0.3, scikit-learn==1.9.1, pandas==3.0.6, numpy==2.5.3, joblib==1.6.0, pytest==9.1.1, python-dotenv==1.2.3, httpx==0.28.1, scipy==1.18.1.
