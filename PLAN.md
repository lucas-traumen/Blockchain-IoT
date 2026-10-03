# PLAN — HRC Safety Log: TV3 + TV4 (RF baseline → LSTM)

Triển khai và xác minh phạm vi TV3 (ESP32, ingestion, session, hiệu chuẩn,
thuật toán ngưỡng) và TV4 (dataset theo session, LSTM dự báo, baseline,
đánh giá và runtime). Repository đã có baseline RF chạy được; kế hoạch này
không xóa RF ngay mà chuyển dần sang LSTM theo các mốc nhỏ, có rollback.
TV1–TV2 (Smart Contract, canonicalize, hash, dashboard và bộ gom sự kiện
chính thức) nằm ngoài phạm vi; TV3/TV4 chỉ bàn giao assessment/event package.

**Trạng thái kế hoạch: phần mềm đến M16/V1, M4j sensor log config/task startup (sửa M4i), M17 Docker và M18 session-boundary/ingestion đã triển khai/kiểm chứng ngày 2026-10-03. Firmware build PASS ESP-IDF 6.0.1 cho ESP32 và ESP32-S3; MQTT/backend Docker đang healthy. Bản firmware mới, đo/hiệu chuẩn/dữ liệu thật, train model thật và soak trên bo còn mở. Phiên này triển khai/review/test trực tiếp, không dùng subagent.**

## 1. Kiến trúc

```
ESP32 + HC-SR04 ──MQTT QoS1──> Mosquitto ──> backend/ingestion (validate → lưu raw SQLite → compute)
                                               ├─ backend/safety   (Sp, CLEAR/VIOLATION/UNKNOWN)
                                                ├─ backend/inference (LSTM hoặc RF legacy → 5 horizon)
                                                └─ processed_samples + event_packages (API bàn giao TV2)
ai_model/  : windows/features dùng chung train+runtime, prepare/split, scaler,
             baselines, train/evaluate LSTM, RF legacy adapter
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

## 2b. Baseline trước khi chuyển LSTM (lịch sử; trạng thái mới ở mục 6/8)

- M1–M6 đã có code backend/safety, ingestion SQLite WAL, session, firmware
  ESP-IDF source, RF pipeline, runtime inference và 147 test pass tại thời điểm
  lập kế hoạch.
- Tại baseline M6, firmware ESP32/HC-SR04 chưa build/flash vì chưa có
  toolchain. Ngày 2026-10-02, M4f đã build PASS trên ESP-IDF 6.0.1 cho
  ESP32-S3; flash/chạy đo vẫn chưa kiểm chứng. Không ghi nhận hardware DoD
  hoàn thành chỉ từ log build.
- `.venv` backend dùng Python 3.12.3, NumPy 2.5.3 và không cài TensorFlow.
  P0 đã kiểm chứng trong environment tạm: TensorFlow 2.20.0 + Keras 3.15.1
  tương thích thực tế, model `(10,2)→(5,)` fit/predict/save/load pass. Quyết
  định vận hành: dùng environment AI riêng (`tensorflow==2.20.0`) để không
  phình/can thiệp `.venv` backend; chỉ đưa TensorFlow vào backend runtime nếu
  M13 chứng minh cần thiết.
- Tại baseline M6, pipeline dùng `d0..d9`, `model.joblib`, `rf-v1`; chưa có tensor
  `(10, 2)`, scaler train-only, `model.keras`, metrics RMSE/F1/lead-time đầy đủ,
  hay runtime `predicted_violation_ms`.
- Chưa coi dữ liệu synthetic là dữ liệu đo thật. Calibration `Z_d`, event
  debounce/pre-post raw package và kiểm chứng broker/board sẽ là các gate riêng.

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

- Cửa sổ kết thúc tại `t`: `X = [d(t-9Δ), ..., d(t)]`,
  `y = [d(t+Δ), ..., d(t+5Δ)]`, `Δ=0.1 s`; horizons
  `[100,200,300,400,500] ms` tính từ mẫu cuối của input. Mỗi timestep của
  LSTM là `[distance_m, delta_distance_m]`, với `delta[0]=0` cố định ở cả
  train và runtime. Shape bắt buộc: `(batch, 10, 2)` → `(batch, 5)`.
- Windowing/feature **dùng chung train + runtime**: `ai_model/windows.py` và
  `ai_model/features.py`; cùng session/device/boot, sequence liên tiếp, mẫu
  hợp lệ, `|dt-0.1| <= 0.03 s`, `dt <= 0.25 s`. Không tạo cửa sổ qua session,
  boot, gap hoặc mẫu lỗi; phải xuất thống kê drop theo nguyên nhân.
- Model đích: Keras LSTM 32 units → Dropout 0.2 → Dense 32 ReLU → Dense 5,
  Adam `1e-3`, Huber, EarlyStopping/ReduceLROnPlateau/ModelCheckpoint theo
  `configs/model.yaml`. RF `rf-v1` được giữ như legacy/rollback và phải có
  adapter riêng nếu representation window chuyển sang tensor; không âm thầm
  trộn artifact RF với artifact LSTM.
- Split theo **session trước khi tập hợp tensor**: 70/15/15, seed và danh sách
  session trong `split_manifest.json`; validation chọn cấu hình, test chỉ báo
  cáo. Scaler distance/delta chỉ fit trên train; output distance inverse-transform
  về mét trước metric/runtime threshold.
- Artifact đích `ai_model/artifacts/lstm-v1/`: `model.keras`, `scaler.json`,
  `manifest.json`, `split_manifest.json`, `metrics.json`; manifest phải có
  version, shape, features, units, horizons, seed, TensorFlow version, dataset
  SHA-256 và split-manifest SHA-256. Artifact không commit.
- Inference runtime: buffer riêng theo `(device_id, boot_id, session_id)`;
  mẫu 1–9 → `WARMUP`, lỗi/gap/jitter → `INVALID_WINDOW`, artifact hỏng/thiếu
  → `MODEL_UNAVAILABLE`, mẫu 10 trở đi → `OK`. Tiền xử lý phải là cùng code
  với train, không dùng pandas cho LSTM runtime, không round trước khi so với
  `Sp`. `predicted_violation_ms` là horizon đầu tiên có `d_hat <= Sp` hoặc
  `null`.
- Safety hiện tại luôn có ưu tiên: lỗi sensor → `UNKNOWN`; distance hiện tại
  `<= Sp` → `VIOLATION`; AI chỉ tạo `PREDICTED_WARNING` khi hiện tại còn
  `CLEAR`. Model hỏng không được làm backend/safety chết.
- Đánh giá bắt buộc MAE **và RMSE theo từng horizon** cho LSTM, persistence,
  constant-velocity; cảnh báo phải có precision, recall, F1, false-warning
  rate, miss rate và lead time (ms), tổng hợp và theo scenario/target_type.
  Không có event thì metric tương ứng là `null`, không bịa; kết quả LSTM thua
  baseline phải được báo cáo nguyên trạng.

## 5. Milestones đã hoàn tất

| ID | Nội dung | Files chính | Kiểm tra |
|----|----------|-------------|----------|
| M1 | Scaffold + schemas + configs + module safety + test safety | `requirements.txt`, `.env.example`, `configs/*`, `schemas/*`, `backend/safety/*`, `tests/test_safety.py` | `pytest tests/test_safety.py -q` |
| M2 | Ingestion core: validate, store, dedup, gap/late, catch-up, ingest_jsonl + test | `backend/ingestion/*`, `scripts/ingest_jsonl.py`, `tests/test_ingestion.py` | `pytest tests/test_ingestion.py -q` |
| M3 | Sessions (control MQTT + fallback), CLI, FastAPI, replay + test | `backend/sessions/*`, `backend/main.py`, `scripts/session_cli.py`, `scripts/replay.py`, `tests/test_sessions.py`, `tests/test_api.py` | `pytest tests/test_sessions.py tests/test_api.py -q` |
| M4 | Firmware ESP-IDF + Wi-Fi macro/mảng hai chiều | `iot_code/esp32/**`, `tests/test_firmware_*.py` | M4j: build IDF 6.0.1 PASS ESP32/S3, 65 native firmware cases; bản sửa mới chưa flash/chạy đo |
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
| M4 | done (code + build) | M4j: Wi-Fi macro 2D, distance log/config fallback/task startup, MQTT TX worker/priority/reserve/SUBACK retry/PUBACK watchdog; 65 native cases PASS; IDF 6.0.1 build ESP32/S3 PASS. Serial/NVS chỉ mode cũ; bản sửa mới chưa flash/chạy F2/F3 trên bo |
| M5 | done | M5a windows/features (24 test) + M5b synthetic dataset/split (12) + M5c train RF + baselines + artifact (10) + M5d1 events logic (22) + M5d2 evaluate CLI (1) — 134 passed tổng. Kết quả thật trên dataset synthetic smoke (12×80): RF MAE [0.017..0.084]m thắng persistence, thua constant-velocity [0.013..0.072]m; event metrics precision 1.0 / recall 0.5 / lead 0.1s. M5d nguyên khối rỗng 1 lần → tách M5d1/M5d2 chạy ngay (bài học flash đúng lần 2) |
| M6 | done | M6a1 predictor (backend/inference/predictor.py) + M6a2 test (10) + M6b tích hợp _compute_and_store + main.py + replay/ingest_jsonl --model-dir + test_integration (3) — 147 passed. Orchestrator tự chạy E2E độc lập: WARMUP/OK/VIOLATION+early_warning/replay offline idempotent đều đúng. sp None → inf (early_warning False, không bịa). Predictor CLI là instance riêng từng lần chạy |
| M7 | done (docs baseline) | README/docs wiring, data collection, TV2 handoff và experiments đã được cập nhật; `scripts/device_sim.py` là gap lúc M7, đã bổ sung ở M16 |
| P0 | done | Environment tạm TensorFlow 2.20.0/Keras 3.15.1 trên Python 3.12.3/NumPy 2.5.3; fit/predict/save/load smoke pass; không sửa repo; backend 147 test pass |
| M8 | done (PASS WITH NOTES) | Config LSTM + RF legacy, assessment fields, model manifest và 20 contract tests; full suite 167 pass. Date-time `FormatChecker` chưa enforce vì env thiếu validator; các note còn lại chuyển sang M9/M11/M13 |
| M9 | done (PASS WITH NOTES) | LSTM feature API `(N,10,2)`, distance+delta, exact config contract, finite/malformed guards, RF không hồi quy; full suite 183 pass |
| M10 | done (PASS WITH NOTES) | Dataset JSONL/generic theo session, split trước windowing, scaler train-only và hash helpers; targeted 53 pass, full suite 208 pass, independent smoke 9/9. Hai minor fail-loud/documentation notes không chặn M11 |
| M11a | done (PASS WITH NOTES) | Training LSTM lazy-TensorFlow + 5 artifact (`model.keras`, `scaler.json`, `split_manifest.json`, `manifest.json`, `metrics.json`), schema/hash/shape loader và staging; backend 214 passed/8 skipped, AI env 13 passed/1 skipped, smoke độc lập 36/36. Không commit artifact; còn minor metrics-integrity/zero-ratio/config-read/import-boundary notes |
| M12a | done (PASS WITH NOTES) | Metric primitives MAE/RMSE, grouped regression, extended event metrics (F1/FWR/miss/lead ms) với null/finite semantics; targeted 55 pass, full 236 pass/8 skipped; smoke độc lập 36/36. Ba minor test/documentation notes không chặn M12b |
| M12b | done (direct) | Lazy-import test, train/evaluate CLI, artifact metrics/hash/config gate, cùng test split RF/LSTM, MAE/RMSE/event/breakdown; AI thật pass |
| M13 | done (code) | MODEL_TYPE chọn LSTM/RF; shared preprocessing, unrounded threshold/horizon, fail-safe, session replay, restart prime; API + MQTT artifact thật pass |
| M14 | done (code) | Debounce 2/3, first assessment, state/event/processed atomic, deterministic ID, raw bracket 2s/1s + coverage; 10 event-package tests pass |
| M15 | done (tool) | Calibration bias/MAE/sample sigma/proposal Zd theo điều kiện; source explicit, không tự sửa config. Board calibration NOT VERIFIED |
| M16 | done | Simulator MQTT control/ack + labelled recording, closed-session export, requirements-ai và docs |
| V1 | software verified | Sau M4j: backend 375 passed/11 skipped; AI Docker 58 targeted passed; full AI trước đó 321 passed/0 skipped. Self-review trực tiếp, hardware/real-data acceptance còn mở |
| M17 | done (Docker) | MQTT/backend healthy, named-volume persistence, broker-only restart và 50 raw/boot + device_ack PASS; AI image integration 58 pass |
| M18 | done (direct) | MQTT raw commit đồng bộ trước ACK; compute safety/AI/event tuần tự ngoài paho thread; START/STOP boundary chờ ACK không lệch session; fallback stop nhìn raw pending; 379 passed/11 skipped |
| M3c | done | Fix đủ 8 finding review M1–M3 (double-stop 409, latest_raw theo boot mới, pin NULL-boot + boot_change, .env, catch_up CLI/mqtt_listener, PK conflict 409, .env.example, session_stats bỏ hardcode). 65 passed (55+10 test mới) |

Phiên bản dependency đã cài trong `.venv` (pin vào requirements.txt ở M1): paho-mqtt==2.1.0, fastapi==0.141.1, uvicorn==0.53.0, pydantic==2.13.5, jsonschema==4.26.0, PyYAML==6.0.3, scikit-learn==1.9.1, pandas==3.0.6, numpy==2.5.3, joblib==1.6.0, pytest==9.1.1, python-dotenv==1.2.3, httpx==0.28.1, scipy==1.18.1.

M8 notes cần giữ cho milestone sau:

- `configs/model.yaml` có khối `lstm` riêng; key flat RF (`model_type`,
  `model_version`, `preprocessing_version`, `features=d0..d9`) giữ nguyên để
  rollback. Khối LSTM dùng `lstm_units` để không nhầm với `units: "m"` trong
  manifest (đơn vị khoảng cách).
- `assessment_v1` thêm optional `preprocessing_version` và
  `predicted_violation_ms`; schema vẫn tương thích assessment RF cũ. M13 phải
  phát hành `preprocessing_version` khi model LSTM OK và tính mốc violation từ
  danh sách horizon, không copy giá trị fixture một cách máy móc.
- `model_manifest_v1` bắt buộc hash model, dataset và split manifest. M11a đã
  bổ sung `scaler_sha256`, nhưng giữ field này optional ở schema để fixture M10
  cũ vẫn validate; trainer và loader LSTM đều ghi/đòi hash scaler. `metrics.json`
  chưa có hash ở M11a; M12b đã bổ sung metrics_sha256 cho artifact mới và
  parse/finite/version/count check cả artifact cũ. Không dùng metrics làm input runtime.
- `jsonschema.FormatChecker` trong `.venv` không có checker `date-time` vì
  thiếu package RFC3339; nếu cần enforce thời gian ở artifact/runtime, dùng
  validator đã đăng ký checker (hoặc bổ sung dependency có kiểm chứng), không
  coi annotation hiện tại là bằng chứng format hợp lệ.
- M9 đã khóa producer tensor: `lstm.features` nếu khai phải đúng chính xác
  `[distance_m, delta_distance_m]`; block malformed/null window size và giá trị
  không hữu hạn bị từ chối, không fallback thành số 0. Minor còn lại (docstring
  về `lstm: null`, type/positive check window size) không chặn M10 và phải giữ
  trong đầu vào validation của M10.
- M10 đã khóa boundary chuẩn bị dataset: `build_lstm_dataset` split theo
  session **trước** khi gọi windowing, inject bản sao `session_id` từ
  `meta.session_label` cho JSONL và reject conflict/row mồ côi/empty split/
  zero-window. `LSTMScaler` fit train-only, lưu `pp-lstm-v1` JSON và inverse
  target về mét; `canonical_json`/SHA-256 helpers tạo hash deterministic cho
  dataset, scaler và split manifest. Các caller SQLite/export phải chuyển row
  thành mapping và gắn session boundary trước khi gọi API; module không tự mở
  database. Bằng chứng: targeted 53 pass, full suite 208 pass, independent
  smoke 9/9, review PASS WITH NOTES (0 blocker/major). Hai minor còn lại là
  fail-loud/documentation, không chặn M11.
- M11a đã khóa đường train/artifact LSTM trong `ai_model.lstm_train` mà không
  đổi `ai_model.train` RF: TensorFlow import lazy, config LSTM điều khiển kiến
  trúc/callback, scaler chỉ fit train, metric MAE tính sau inverse-transform về
  mét, manifest/schema/timestamp/hash được validate trước promote staging. API
  chính là `train_lstm(...)` và `load_lstm_artifact(...)`; artifact chỉ sinh ở
  thư mục operator truyền vào và không commit. Review không có blocker/major;
  minor đã ghi nhận: zero-ratio val/test nên fail sớm hơn, `lstm.metrics` đang
  khóa thực tế về MAE, trainer còn import `backend.util`, và loader chưa hash/
  parse nội dung `metrics.json`. Các điểm này không chặn offline evaluation,
  nhưng phải được cân nhắc trước M13 runtime.
- M12a đã thêm metric primitives thuần trong `ai_model.evaluate` và
  `ai_model.events`: `rmse_per_horizon`, `regression_metrics`, grouped
  regression/event helpers và `event_metrics_extended`. API mở rộng trả số
  hữu hạn hoặc `None` (không dùng `inf` cho extended report), nhưng API
  `event_metrics` legacy giữ nguyên output/semantics để không phá M5. Bằng
  chứng: targeted `55 passed`, full `236 passed, 8 skipped`, smoke độc lập
  `36/36`; reviewer 0 blocker/major. Minor cần gom vào M12b: thêm test pin
  legacy `inf`, test JSON serialization ở zero-span extended branch, và test
  explicit group không có event. M12a chỉ là primitives; M12b đã hoàn tất wiring qua API riêng
  `evaluate_lstm_models` và CLI `--model-type lstm`, giữ API RF cũ.

## 7. Lộ trình đã được duyệt — trạng thái mới ở mục 6/8

Các task dưới đây là frontier sau baseline. Chỉ dispatch một coder tại một
thời điểm; mỗi task sẽ được review/test độc lập trước khi chốt. Nếu một task
phát sinh quá nhiều file, orchestrator sẽ tách tiếp thay vì yêu cầu một lần
sinh code lớn.

| Task | Mục tiêu và phạm vi file | Tiêu chí đạt / kiểm tra | Phụ thuộc |
|------|---------------------------|-------------------------|-----------|
| P0:env | Kiểm tra tương thích Python 3.12, NumPy hiện tại và TensorFlow/Keras; quyết định pin `requirements.txt` hoặc environment AI riêng; không phá `.venv` backend | Có lệnh cài tái lập, import TensorFlow + train/predict smoke hoặc ghi rõ environment riêng; baseline `pytest tests/ -q` vẫn pass | — |
| M8:contract | Cập nhật `configs/model.yaml`, schema assessment (preprocessing/model metadata, `predicted_violation_ms`), model-version contract; giữ RF legacy rõ ràng | Schema validate cả assessment cũ và mới theo quyết định tương thích; config đủ window/features/LSTM/train params; test contract | P0 |
| M9:window | Chuyển representation LSTM sang `(10,2)` `[distance_m, delta_distance_m]`, `delta[0]=0`; giữ adapter RF d0..d9; sửa `ai_model/windows.py`, `features.py` và test | Không nối qua session/boot/gap/error; shape/order/delta đúng ở train-runtime; test window cũ không hồi quy | M8 |
| M10:data (DONE) | Hỗ trợ JSONL + export session/SQLite, split session trước window aggregation, scaler train-only, hash dataset/split; `ai_model/prepare.py` và module preprocessing mới | Không leakage; scaler không đọc val/test; dropped-window stats; `split_manifest.json` và SHA-256 tái lập; 208 test pass | M9 |
| M11a:train (DONE) | Bổ sung Keras LSTM baseline theo kiến trúc chốt; EarlyStopping, ReduceLR, checkpoint; lưu 5 artifact; giữ RF train path | Output `(N,5)`, artifact đủ file/field/version/hash, loader fail-loud, smoke cross-environment pass; 214 backend tests + 36/36 independent checks | M10, P0 |
| M12a:metrics (DONE) | Thêm MAE/RMSE primitives, grouped helpers và event metrics mở rộng mà không phá API M5 | Shape/nonfinite guards, finite/null JSON semantics, F1/FWR/miss/lead ms; 55 targeted + 236 full pass | M11a |
| M12b:evaluate (DONE software) | Wiring evaluator LSTM/RF/persistence/constant-velocity trên cùng session test split; RMSE/MAE từng horizon, event metrics extended và breakdown scenario/target_type | Báo cáo test theo session; không tune test; metric `null` khi không xác định; không che trường hợp LSTM thua baseline; artifact LSTM chỉ đọc qua loader/hash gate | M12a, M11a |
| M13:runtime (DONE software) | Cập nhật `backend/inference/predictor.py`, `backend/main.py`, ingestion và scripts replay: buffer LSTM, preprocessing giống train, `predicted_violation_ms`, WARMUP/INVALID/MODEL_UNAVAILABLE; RF fallback tách biệt | 9→WARMUP, mẫu 10→OK, reset gap/error/boot/session, inverse scaling không round trước threshold, safety sống khi model hỏng | M11, M8 |
| M14:event (DONE software) | Bổ sung debounce AI 2/3, assessment/event trigger và gói pre/post raw tối thiểu cho bàn giao TV2; không canonicalize/hash thay TV2 | Trigger không trùng, lưu assessment đầu tiên, đủ raw 2 s trước + 1 s sau khi có dữ liệu; test replay/idempotency | M13 |
| M15:calibration (DONE software) | Tạo công cụ/format báo cáo hiệu chuẩn HC-SR04 và đề xuất `Z_d` từ bias/MAE/σ; không tự ghi đè safety config demo | Tính đúng công thức, giữ điều kiện thử/target/session, phân biệt demo với thực nghiệm; test dữ liệu mẫu + tài liệu thao tác | M2, M7 |
| M16:docs (DONE software) | Cập nhật README/docs theo LSTM, environment AI, dataset thật, wiring, TV2 handoff; thêm simulator nếu còn thiếu | Mọi lệnh docs chạy được hoặc ghi rõ human/hardware gate; không tuyên bố ISO/board đã xác minh | M12–M15 |
| V1:review (software verified) | Reviewer đọc diff/spec; tester chạy full suite, smoke offline/API và (nếu có toolchain) firmware build; orchestrator kiểm tra diff + evidence | 0 blocker/major chưa xử lý; full test pass; báo riêng phần chưa kiểm chứng trên phần cứng/broker | M16 |

### Thứ tự thực hiện và gate

1. **P0 → M8 → M9 → M10**: khóa dependency, contract và dữ liệu trước khi
   đụng model. Nếu TensorFlow không tương thích NumPy/Python hiện tại, dừng
   tại P0 để chốt environment riêng, không sửa mù requirements.
2. **M11 → M12**: train/evaluate offline trước; chưa tích hợp runtime nếu
   artifact không qua shape/manifest/hash gate. Kết quả trên synthetic chỉ là
   smoke test, không thay cho dataset HC-SR04 thật.
3. **M13 → M14**: runtime và event chỉ dùng model đã version hóa; safety
   hiện tại độc lập và luôn là nguồn trạng thái VIOLATION/UNKNOWN.
4. **M15 → M16 → V1**: calibration/hardware/docs là gate nghiệm thu. Không
   đánh dấu TV3 hardware Done nếu chưa có log build/flash/đo thật; không gọi
   tham số demo là giá trị ISO.

### Gate phần cứng còn cần cấu hình/đo thật

- ESP-IDF 6.0.1 build PASS cho ESP32 và ESP32-S3. Trong phiên đã nhận diện
  ESP32-D0WD-V3 revision v3.1 và S3 revision v0.2; chọn lại chip/cổng thực tế
  trước flash. Người dùng đã gửi log HRC chạy thật nhưng bản sửa M4j chưa được
  nạp bởi Codex. Wi-Fi/broker dùng macro trong local header; đo điện Echo,
  F2/F3, radio, SNTP, thời lượng chu kỳ, reconnect/soak và session real trên
  bo vẫn chưa kiểm chứng.
- Nếu người dùng chỉ yêu cầu code/pipeline, V1 sẽ chốt các mục trên là
  `NOT VERIFIED`, kèm lệnh và biểu mẫu để chạy ở phòng thí nghiệm; không suy
  diễn thành PASS.

## 8. Tiếp tục trực tiếp — 2026-10-02

Người dùng đã duyệt tiếp tục plan và yêu cầu Codex tự triển khai, không dùng
subagent. Phiên này sửa code, tự review và chạy test trực tiếp; không mở
OpenCode hay dispatch lại session cũ. Tool checkpoint OpenCode không có trong
phiên Codex; PLAN/PROJECT_MEMORY ghi evidence của phiên này, checkpoint cũ
chỉ phản ánh trạng thái phiên OpenCode đến 2026-09-30. Phiên sau ưu tiên
trạng thái mới trong mục này; không dispatch lại tester cũ chỉ vì checkpoint
đang ghi running. Không sửa JSON checkpoint để giả lập tool.

| Task trực tiếp | Phạm vi file | Tiêu chí/lệnh kiểm tra | Trạng thái |
|---|---|---|---|
| M12b:orchestrator | ai_model/evaluate.py, ai_model/lstm_train.py, ai_model/train.py, ai_model/features.py, ai_model/prepare.py, ai_model/preprocessing.py, schemas/model_manifest_v1.schema.json, tests/test_lstm_*.py, tests/test_evaluation.py | Evaluator + CLI chung split, lazy import, hash/shape/finite gate; backend pytest + AI env integration | done |
| M13:orchestrator | backend/inference/*, backend/settings.py, backend/main.py, backend/ingestion/{service.py,mqtt_listener.py}, scripts/replay.py, scripts/ingest_jsonl.py, .env.example, requirements-ai.txt, tests/test_lstm_inference.py | Preprocessing train/runtime giống nhau; reset boundary; fail-safe; API/replay test và artifact thật | done (software) |
| M14:orchestrator | backend/events/*, backend/ingestion/{schema.sql,store.py,service.py}, backend/main.py, schemas/event_package_v1.schema.json, tests/test_event_packages.py | Debounce 2/3, sự kiện idempotent, pre 2s/post 1s cùng session/boot, persistence/recovery | done (software) |
| M15:orchestrator | scripts/calibrate.py, tests/test_calibration.py, docs/calibration.md | Bias/MAE/sigma/Zd, nhãn nguồn/điều kiện thử, không tự sửa safety config | done (software) |
| M16:orchestrator | README.md, docs/*, scripts/device_sim.py, scripts/export_dataset.py, tests/test_device_sim.py, tests/test_export_dataset.py, .gitignore | CLI/docs/simulator hoạt động, hardware gates rõ | done (software) |
| V1:orchestrator | PLAN.md, PROJECT_MEMORY.md; review các diff trên | Full suite backend + AI, smoke offline/API, báo hardware/real-data/reconnect gate chưa kiểm chứng | done (software) |

### Evidence chốt ngày 2026-10-02

- `.venv/bin/python -m pytest tests/ -q`: **310 passed, 11 skipped**, 2 dependency
  deprecation warnings; skip chỉ các integration cần TensorFlow.
- `TF_CPP_MIN_LOG_LEVEL=3 TF_NUM_INTEROP_THREADS=1 TF_NUM_INTRAOP_THREADS=1
  OMP_NUM_THREADS=1 /tmp/blockchain-ai-env/bin/python -m pytest tests/ -q`:
  **321 passed, 0 skipped**, 41 dependency deprecation warnings (FastAPI
  TestClient/httpx và Keras/NumPy); không có test failure.
- Môi trường AI tái lập bằng `python3 -m venv /tmp/blockchain-ai-env` rồi pip
  `-r requirements-ai.txt`; `.venv` backend không cài TensorFlow.
- CLI train LSTM 3 epochs + evaluator (LSTM/RF/persistence/CV) chạy thật trên
  synthetic 12 session × 80 mẫu, seed 42, 96 test windows, test sessions
  `sess_2`, `sess_8`. RF smoke grid `[10] × [4]`, cùng dataset/split; không tune test.
- Live smoke Mosquitto `eclipse-mosquitto:2.0.22` tại cổng riêng 11883:
  **70 telemetry**, **50 session samples**, start/stop `device_ack`, ranh giới
  sequence **6..55**. LSTM prediction: WARMUP 37, OK 31, INVALID_WINDOW 2;
  safety: CLEAR 57, VIOLATION 11, UNKNOWN 2. Ba event package trên raw đã lưu
  đủ coverage sau khi thêm sample bracket biên cho jitter; event UNKNOWN×2,
  VIOLATION×1. Không suy diễn thành có PREDICTED_WARNING từ model smoke này.
- Replay cùng DB/model: lần đầu **70 processed** (57 CLEAR/11 VIOLATION/2
  UNKNOWN), lần hai **0 processed**. Instance/model buffer riêng, session
  boundaries được giữ; history live không ghi đè.
- Raw/artifact/report bằng chứng ở `/tmp/blockchain-validation/`, không commit;
  broker test đã dừng/xóa container, không can thiệp broker/ứng dụng đang chạy.
- `git diff --check` pass. Tự review contract/data/safety/persistence/CLI theo
  yêu cầu không subagent; không gọi đó là review độc lập của reviewer cũ.

Kết quả evaluator synthetic (trung bình 5 horizon, mét):

| Model | MAE mean | RMSE mean | Event recall @500ms |
|---|---:|---:|---:|
| LSTM (3 epochs smoke) | 0.097709 | 0.125999 | 0.0 |
| RF (smoke grid) | 0.056047 | 0.070198 | 0.0 |
| Persistence | 0.056925 | 0.062659 | 0.0 |
| Constant velocity | 0.040475 | 0.060800 | 1.0 |

LSTM smoke **thua baseline**; đây chỉ là bằng chứng pipeline train/load/
evaluate/runtime hoạt động, chưa phải model nghiệm thu. Không tăng epoch/tune
hyperparameter dựa trên test này để sửa kết quả báo cáo.

### Frontier còn lại

1. M4j build PASS ESP32/S3, có Wi-Fi macro, distance log và MQTT recovery.
   Điền local header, nạp đúng target/cổng, chạy F2/F3 và đo Echo/nhiễu trên
   bo thật; không coi native mocks là xác minh radio/phần điện.
2. Thu session **real**, hiệu chuẩn từng điều kiện, xem xét Zd và phát hành
   safety config version mới; công cụ đã có, chưa có số đo thật.
3. Train/chọn model bằng train/validation trên real dataset, báo test cố định
   và so baselines; chưa chốt LSTM thắng baseline hay thông số robot đạt ISO.
4. Docker persistence và backend reconnect sau restart riêng broker đã PASS
   (M17). Reconnect khi mất mạng và soak trên ESP32 thật vẫn chưa kiểm chứng.
   M18 đã khóa race START/STOP bằng worker compute tuần tự và boundary gate;
   raw commit trước ACK, assessment/session/predictor/event dùng cùng session scope.
   DB thật hiện có 67.930 raw, 7 boot và 0 session — chưa thể train trước khi
   người vận hành mở/đóng các session có kịch bản và nhãn thật.
5. TV2 tiêu thụ/canonicalize/hash/gửi chain và dashboard ngoài phạm vi repo.

## 9. M4f — sửa build với ESP-IDF đã cài (2026-10-02)

| Task trực tiếp | Mục tiêu/phạm vi | Tiêu chí và evidence | Trạng thái |
|---|---|---|---|
| M4f:orchestrator | iot_code/esp32/{CMakeLists.txt,sdkconfig.defaults,dependencies.lock,main/CMakeLists.txt,main/idf_component.yml,main/telemetry.c,.vscode/settings.json,README.md}, .gitignore và docs/plan/memory liên quan | `idf.py -C iot_code/esp32 set-target esp32s3` + `build` exit 0; app/bootloader/partition size checks PASS; `esptool --port /dev/ttyACM0 chip-id` nhận diện ESP32-S3 revision v0.2 | done (build); flash/runtime chưa kiểm chứng |

- Lỗi đã tái hiện: CMake không resolve `mqtt` trong IDF 6.0.1, sau đó GCC
  `-Werror=format-truncation` tại telemetry.c khi bọc timestamp bằng quote.
- Thêm managed MQTT `espressif/mqtt` chỉ khi IDF >= 6.0, resolve/pin 1.0.0
  trong `dependencies.lock`; IDF 5.5 giữ bundled MQTT. Khai báo trực tiếp
  `esp_driver_gpio`, `esp_driver_rmt`, `lwip`, `esp_hw_support`.
- Buffer timestamp JSON có thêm hai byte cho quote; giữ strict compiler
  warnings. Board nối máy là ESP32-S3 nên default/generated target đổi sang
  `esp32s3`; VS Code flash type UART, port giữ `/dev/ttyACM0`.
- Build log `/tmp/blockchain-firmware-check/idf-build.log`: đầy đủ app,
  bootloader và partition table; app **915104 bytes**, partition app 1 MiB
  còn 13%, bootloader còn 36%. Không có compiler warning/error trong log.
- `sdkconfig` và `sdkconfig.old` không commit; chưa điền Wi-Fi SSID/password.
  Chưa flash firmware HRC, chưa đo HC-SR04 hay xác minh mạng trên board.
  IDF 5.5 chưa được build trong phiên này; không nâng trạng thái hardware DoD.


## 10. M4g + M17 — serial Wi-Fi, ổn định lỗi và Docker (2026-10-02)

Người dùng yêu cầu firmware có list Wi-Fi và host MQTT/các service chạy Docker.
Tiếp tục triển khai trực tiếp theo ủy quyền, không subagent/checkpoint giả.

| Task trực tiếp | Phạm vi file | Tiêu chí/lệnh kiểm tra | Trạng thái |
|---|---|---|---|
| M4g:orchestrator | iot_code/esp32/main/{wifi_mqtt,network_setup,network_console,diagnostics,hcsr04,telemetry,app_main}.*, main/{CMakeLists.txt,Kconfig.projbuild}, tests/firmware/**, tests/test_firmware_*.py, firmware README và docs liên quan | Scan/list, masked join, NVS commit sau IP, rollback join lỗi, retry, broker đổi runtime, sensor/enqueue fault/recovery; UART/USB receive tests; IDF build esp32s3 + esp32; full pytest | done (code/build/native); board acceptance mở |
| M17:orchestrator | Dockerfile, .dockerignore, compose.yaml, compose.env.example, docker/mosquitto/mosquitto.conf, .gitignore, README.md, docs/{docker,wiring,data-collection}.md, PLAN.md, PROJECT_MEMORY.md | Compose config/build/up PASS; mqtt/backend healthy; telemetry/control/ack, SQLite + MQTT persistence và broker-only reconnect; AI TensorFlow integration | done (Docker); service tiếp tục chạy |

### Firmware và test

- UART commands `wifi`/`wifi scan`, `join N`, `hidden`, `broker mqtt://HOST:PORT`,
  `status`, `help`; tự scan khi chưa có IP. Hỗ trợ UART và native USB
  Serial/JTAG của S3 (console chính hoặc phụ); USB CDC/TinyUSB chưa có adapter.
- Wi-Fi chỉ lưu blob NVS `hrc_net/wifi` sau khi GOT_IP; failed join không ghi
  đè và restore previous runtime config. Dùng WIFI_STORAGE_RAM. Broker NVS
  ưu tiên trước fallback Kconfig, đổi qua serial sẽ stop/set-URI/start MQTT.
- Wi-Fi retry 1/2/4/8/16/30s; MQTT start deferred đến GOT_IP, timeout3s và
  reconnect10s. Outbox10240 byte vẫn có drop khi đầy/quá hạn, không có durable
  offline storage. Status có IP/broker/outbox, sensor GPIO và tổng invalid/drop.
- Lỗi sensor/enqueue lặp lại tổng hợp 5s, vẫn báo transition/recovery. Reset
  receiver khi no recv_done/TRIG/start lỗi; mẫu invalid vẫn UNKNOWN. Task đo
  bỏ deadline quá hạn để tránh phát dồn. Network/console sống khi sensor init lỗi.
- Native tests compile actual C modules với `-Wall -Wextra -Werror`:
  setup12, network10, sensor6, UART/USB adapter6 = **34 pass**. Wi-Fi/NVS,
  GPIO/RMT và adapter I/O là giả lập, chưa chứng minh sự kiện bất đồng bộ
  trên chip, độ chính xác đo, mạch Echo hoặc reconnect khi mất radio thật.
- `.venv/bin/python -m pytest tests/ -q`: **344 passed, 11 skipped**, 2
  dependency deprecation warnings; 11 skip là test cần TensorFlow.
- ESP32-S3 build `iot_code/esp32/build/hrc_esp32.bin`: **947680 bytes**, app
  1MiB còn10%; log `/tmp/blockchain-firmware-check/serial-setup-build.log`.
- ESP32 build riêng `/tmp/blockchain-firmware-esp32/build/hrc_esp32.bin`:
  **935776 bytes**, app còn11%; log `/tmp/blockchain-firmware-check/esp32-build.log`.
  Không thay sdkconfig/lock target S3 trong repo để kiểm tra ESP32.
- `esptool --port /dev/ttyUSB0 chip-id`: **ESP32-D0WD-V3 revision v3.1**,
  CP2102. Không flash binary S3 vào chip này. Chưa nạp bản sửa lên bo;
  đọc serial 3s không thấy output, không kết luận cảm biến/USB đã ổn.

### Docker và smoke thực tế

- Project `blockchain-hrc`, Mosquitto2.0.22 host1885→1883; backend API8001→8000,
  SQLite volume `backend_data`. Giữ các service dự án khác trên1883/1884/8000.
  MQTT volume `mqtt_data`, configs/artifacts backend chỉ đọc, restart/log rotation.
- Dockerfile target `backend` không có TF; target/profile `ai` thêm TF2.20.0 /
  Keras3.15.1. Backend LSTM chọn target ai + MODEL_TYPE=lstm + subdir=lstm-v1.
  Không đưa artifact synthetic smoke vào runtime mặc định; /health hiện
  model_type=rf, model_available=false, safety vẫn tính.
- `docker compose config --quiet`, `build backend`, `--profile ai build ai`,
  `up -d --wait mqtt backend` PASS; hai container healthy. Backend và AI
  `python -m pip --no-cache-dir check` PASS.
- AI image chạy `tests/test_lstm_{train,inference,evaluation}.py` qua mount
  read-only: **58 passed**, gồm train/save/load/predict thật; 41 dependency
  warnings, không failure. `/tmp/blockchain-firmware-check/docker-ai-tests.log`.
- Smoke simulator `--device-id esp32_01`, host127.0.0.1:1885; API8001.
  **50/50 raw sequence1..50** mỗi boot, START/STOP device_ack, các lượt có
  20–21 session samples. Lượt đầu fail stats0 do simulator mặc định esp32_sim
  khác DEVICE_ID, không phải bằng chứng SQLite ingest hỏng.
- Restart mqtt+backend giữ nguyên phiên SQLite 21 mẫu và retained test topic.
  Retained topic test đã clear. Restart **riêng mqtt** cũng PASS control/ack
  và 50 raw sau reconnect của backend (không restart backend để hồi phục).
- Ghi nhận race session hiện có: ack START seq5 nhưng tạo row session sau khi
  seq6 đã processed, session [6..26] chỉ gán7..26 (20 mẫu), raw vẫn đủ50.
  SessionManager đã ghi giới hạn race một mẫu từ baseline; không coi smoke
  này là chứng minh ranh giới session tuyệt đối chính xác. Cần xử lý riêng
  trước nghiệm thu dataset, bao gồm assessment/session/predictor/event scope.
- Smoke evidence/SQLite backup nằm `/tmp/blockchain-firmware-check/`:
  docker-smoke.json, docker-persistence.json, docker-smoke.db. DB chỉ chứa
  synthetic đã được backup thành `/app/data/synthetic-smoke-20261002.db`
  trong volume; DB live `/app/data/hrc.db` mới rỗng để thu số đo thật.
- Host LAN tại lần kiểm tra: **192.168.100.3**, firmware serial broker:
  `broker mqtt://192.168.100.3:1885`. IP có thể đổi khi chuyển mạng.
- Không commit, không sửa global OpenCode hoặc `.opencode/state/`.


## 11. M4h — Wi-Fi macro và mảng hai chiều (2026-10-02)

Người dùng chọn macro, không nhập Wi-Fi qua serial: khai báo danh sách
SSID/password thành mảng hai chiều rồi quét để chọn. Tiếp tục triển khai trực
tiếp theo ủy quyền, không subagent. Mục này thay lựa chọn vận hành Wi-Fi
serial/NVS của M4g; evidence cũ ở mục 10 giữ nguyên theo lịch sử.

| Task trực tiếp | Phạm vi file | Tiêu chí/lệnh kiểm tra | Trạng thái |
|---|---|---|---|
| M4h:orchestrator | main/{network_config.h,network_config.example.h,wifi_mqtt.c,wifi_mqtt.h,app_main.c,Kconfig.projbuild}, local header ignored, tests/{test_firmware_network.py,test_firmware_wifi_list.py,firmware/network_harness.c,firmware/console_harness.c,firmware/stubs/freertos/*,firmware/stubs/idf_stub.h}, .gitignore, README.md, firmware README, docs/{docker,wiring}.md, PLAN.md, PROJECT_MEMORY.md | Mảng 2D macro; scan toàn bộ AP, RSSI mạnh nhất, fallback khi lỗi, rescan/backoff, bỏ qua NVS; native strict compiler; full pytest; build ESP32/S3; local credentials ignored | done (code/build/native); flash/radio trên bo chưa kiểm chứng |

- `main/network_config.local.h` đã tạo nếu chưa có, Git ignore; không ghi đè
  credentials của người dùng. Header mẫu commit được. Macro
  `HRC_WIFI_NETWORKS` khởi tạo `static const char *const s_known_wifi[][2]`,
  mỗi dòng `{SSID, password}`, 1–16 dòng; SSID rỗng/credentials sai bỏ qua.
  `HRC_MQTT_URI` đặt broker; template dùng `mqtt://192.168.100.3:1885`.
- `HRC_WIFI_LIST_MODE=1`: không đọc Wi-Fi/broker NVS cũ, không mở serial
  setup. Scan/select chạy trong task riêng; event/timer callbacks không chờ
  scan/kết nối. Đọc toàn bộ AP records, lọc security, RSSI giảm dần và ưu
  tiên dòng trước khi bằng RSSI. Cho phép SSID trùng với password khác.
- Mỗi candidate đợi IP tối đa 15 s, lỗi disconnect đánh thức sớm rồi thử
  candidate tiếp theo. Hết candidate/mất mạng → scan lại sau 1/2/4/8/16/30 s;
  GOT_IP reset backoff. Init chỉ đợi tối đa 15 s, task chọn mạng có thể tiếp
  tục nền. Danh sách toàn rỗng/sai không tạo vòng reconnect vô hạn; scan lỗi
  bỏ kết quả một phần và giải phóng AP list của SDK. Mật khẩu không log.
- Chế độ cũ `HRC_WIFI_LIST_MODE=0` hoặc không có local header vẫn dùng
  Kconfig/NVS/serial; test fixture tường minh không đọc credentials riêng.
- Test mới: best/fallback/missing/many_aps/full_ssid/open/unsupported/
  duplicate/disconnect/all_fail/scan_failure/blank/invalid/record_failure.
  Tổng native **48 case PASS**; actual C compile `-Wall -Wextra -Werror`.
  Stub FreeRTOS kiểm tra thứ tự include `FreeRTOS.h` trước `task.h`, khớp
  yêu cầu build thật.
- `.venv/bin/python -m pytest tests/ -q`: **358 passed, 11 skipped**, 2
  dependency warnings; skip cần TensorFlow. Log
  `/tmp/blockchain-firmware-check/wifi-list-pytest.log`.
- ESP-IDF 6.0.1 `build` exit 0 cho S3 và ESP32, không compiler warnings/errors;
  app/bootloader/partition checks PASS. S3 app **949680 bytes**, partition
  1 MiB còn 9%; log `/tmp/blockchain-firmware-check/wifi-list-esp32s3-build.log`.
  ESP32 app **937808 bytes**, còn 11%; log
  `/tmp/blockchain-firmware-check/wifi-list-esp32-build.log`. ESP32 build tại
  `/tmp/blockchain-firmware-esp32`, không đổi sdkconfig/lock S3 trong repo.
- `git diff --check` PASS; private local header ignored; MQTT/backend Docker
  vẫn healthy ở cổng 1885/8001. Không commit, không flash, không can thiệp
  serial monitor đang mở. Hardware gate tiếp theo: điền credentials, build/
  flash đúng chip/cổng, kiểm tra scan/IP/MQTT, fallback và reconnect trên bo
  cùng F2/F3/Echo/SNTP/soak trước thu dataset real.


## 12. M4i — log khoảng cách và MQTT đầy outbox/reconnect (2026-10-02)

Người dùng báo outbox 10249 byte dù cấu hình 10240, enqueue mất 215 mẫu,
transport write timeout 3000 ms và subscribe lỗi ngay sau reconnect. Yêu cầu
thêm log khoảng cách và sửa MQTT; triển khai trực tiếp, không subagent.

| Task trực tiếp | Phạm vi file | Tiêu chí/lệnh kiểm tra | Trạng thái |
|---|---|---|---|
| M4i:orchestrator | iot_code/esp32/main/{wifi_mqtt.c,wifi_mqtt.h,telemetry.c,Kconfig.projbuild}, tests/{test_firmware_network.py,test_firmware_telemetry.py,firmware/network_harness.c,firmware/telemetry_harness.c,firmware/stubs/idf_stub.h,firmware/stubs/freertos/{task.h,queue.h},firmware/stubs/esp_random.h}, firmware README, docs/wiring.md, PLAN.md, PROJECT_MEMORY.md | Repro red trên code cũ cho SDK mutex trong task đo và outbox vượt ngưỡng; zero-wait producer, bounded transfer/reserve, subscribe retry/SUBACK gate, PUBACK watchdog, ACK priority, log valid/invalid/disabled; full pytest; ESP32/S3 build; broker probe riêng | done (code/build/native); bản mới chưa flash/soak trên bo |

- Feedback loop trước sửa: `pytest tests/test_firmware_network.py -q -k
  mqtt_nonblocking` FAIL vì task đo gọi SDK enqueue; `-k mqtt_headroom` FAIL
  khi mock giới hạn đúng esp-mqtt (payload-only) làm outbox vượt limit.
  Đã đối chiếu source managed `espressif/mqtt` 1.0.0: enqueue giữ MQTT API
  mutex, limit check chưa tính packet overhead, subscribe trả -2 khi outbox
  vượt limit. Đây là bug quản lý backlog/priority phía firmware; nguyên
  nhân vật lý của socket timeout trên Wi-Fi chưa chứng minh.
- Mailbox zero-wait: telemetry 16, ack 4; worker riêng, stack 4096, priority
  4, mỗi vòng tối đa 4 message và delay 100 ms. Producer copy topic/payload,
  không gọi SDK API hoặc getter có mutex. Ack có queue riêng/ưu tiên trước
  telemetry; ESP_OK nghĩa là accepted trong RAM, không phải delivered.
- SDK outbox 32768 byte, telemetry budget 30720; reserve control/ack 2048,
  ack vẫn chừa 256 cho subscribe. Tính cả header/topic/packet ID; high/low
  watermark tránh oscillation, giữ RAM message khi SDK từ chối enqueue.
  Mất mạng lâu/full mailbox vẫn drop mẫu mới có đếm; RAM/SDK không durable.
- SUBACK QoS1 mới cho telemetry chạy; subscribe lỗi retry 1 s, thiếu SUBACK
  retry 15 s, reject/QoS không đúng không đặt control ready. Callback không
  làm network I/O; subscribe do worker thực hiện. Mỗi reconnect reset state.
- Theo dõi PUBACK: còn gói publish mà không có ACK tiến triển 15 s → request
  disconnect, SDK tự reconnect; không cắt khi ACK vẫn tiến triển. Keepalive
  20 s, retransmit 5 s, reconnect 5 s, timeout mạng 10 s. Cached outbox và
  atomic counters cho log; Wi-Fi GOT_IP khi client đã start không chờ TX mutex.
- Khoảng cách log mỗi 1000 ms độc lập MQTT admission, gồm distance_mm,
  echo_us, valid/error/sequence. `HRC_DISTANCE_LOG_PERIOD_MS`: 100 để từng
  mẫu mặc định, 0 để tắt; không đổi sampling period/schema/UNKNOWN semantics.
- Native tăng từ 48 lên **65 case PASS**: 13 MQTT regressions và 4 telemetry
  cases, strict `-Wall -Wextra -Werror`, actual C. MQTT mock mô phỏng mutex/
  SDK failures/event transitions; không chứng minh radio/network timing thật.
- `.venv/bin/python -m pytest tests/ -q`: **375 passed, 11 skipped**, 2
  dependency warnings. Log `/tmp/blockchain-firmware-check/mqtt-recovery-pytest.log`.
- Build IDF6.0.1 PASS ESP32-S3 (**951840 bytes**) và ESP32 (**939936 bytes**),
  app/bootloader/partition checks PASS; không compiler warning/error. Logs
  `mqtt-recovery-{esp32s3,esp32}-build.log` tại `/tmp/blockchain-firmware-check/`.
  ESP32 tiếp tục build isolated; repo/local credentials/target S3 giữ nguyên.
- Broker probe QoS1 ở `127.0.0.1:1885`, 20/20 PUBACK, p95 3.99 ms/max
  6.63 ms; topic `hrc/diagnostics/mqtt_probe` ngoài ingestion, không tạo
  synthetic raw/session. Cùng phép probe nhận 23 telemetry real, đều valid.
  Evidence `mqtt-recovery-broker-probe.json`. Đây là health tại host/current
  firmware; không phải smoke bản M4i hoặc bằng chứng mạng sẽ không timeout.
- Docker mqtt/backend vẫn healthy, không restart; private header ignored,
  diff check PASS; không commit/flash hay sửa global OpenCode/state. Next:
  nạp bản M4i đúng chip/cổng, xem distance/control ready/PUBACK, thử mất mạng/
  broker/reconnect và F2/F3/Echo/SNTP/soak; real dataset/calibration vẫn mở.


## 13. M4j — sửa cấu hình log cảm biến (2026-10-03)

Người dùng gửi log chạy thật ngày 2026-10-03: Wi-Fi lấy IP `192.168.100.124`,
MQTT `control ready`, HC-SR04 `ready`, nhưng không có log `telemetry` hoặc
`distance_mm`. Đối chiếu binary/config cho thấy `sdkconfig` cũ không có
`CONFIG_HRC_DISTANCE_LOG_PERIOD_MS`; vì telemetry.c dùng `#if` nên compiler
đã loại toàn bộ distance log. Việc này không đủ để kết luận task đo hoặc phần
Echo không chạy — chỉ là log đã bị compile-out.

| Task trực tiếp | Phạm vi file | Tiêu chí/lệnh kiểm tra | Trạng thái |
|---|---|---|---|
| M4j:orchestrator | `iot_code/esp32/{sdkconfig.defaults,sdkconfig}`, `main/{app_main.c,telemetry.c}`, PLAN/MEMORY | Option log mặc định 1000 ms; fallback khi sdkconfig cũ thiếu; log task startup; full pytest; build S3/ESP32 không warning/error | done (source/config/build); chưa flash bản M4j |

- Thêm `CONFIG_HRC_DISTANCE_LOG_PERIOD_MS=1000` vào `sdkconfig.defaults` và
  sdkconfig hiện tại. `sdkconfig` là ignored/runtime file; defaults bảo đảm
  checkout mới có option.
- `telemetry.c` và `app_main.c` có fallback 1000 ms khi sdkconfig cũ không
  định nghĩa option. Giá trị 0 vẫn tắt log nếu người dùng chọn 0 trong Kconfig.
- Sau `xTaskCreate` thành công, app in:
  `telemetry task started; distance log interval=1000 ms`. Sau đó mỗi giây
  telemetry in `distance_mm`, `echo_us`, `valid`, `error`, `sequence`.
- Build S3 `hrc_esp32.bin` **0xe8740 bytes**, app còn 9%; ESP32 isolated
  `hrc_esp32.bin` **0xe58a0 bytes**, app còn 10%. Hai log tại
  `/tmp/blockchain-firmware-check/sensor-log-config-esp32s3-build.log` và
  `sensor-log-config-esp32-build.log`, không compiler diagnostics.
- Full `.venv/bin/python -m pytest tests/ -q`: **375 passed, 11 skipped**,
  2 dependency warnings. `git diff --check` PASS. Chưa flash; hardware
  verification tiếp theo là thấy startup line, distance log/error log và
  xác minh Echo/GND/divider nếu distance vẫn null.


## 14. M18 — khóa boundary session cho pipeline AI (2026-10-03)

Nguyên nhân race đã ghi ở M17 là `session_start` nhận ACK trước khi row
`sessions` được insert; mẫu tiếp theo có thể bị compute với `session_id=null`,
trong khi session sau đó chỉ gán các mẫu về sau. Cách sửa phải giữ cả QoS1 và
phạm vi AI nhất quán.

| Task trực tiếp | Phạm vi file | Tiêu chí/lệnh kiểm tra | Trạng thái |
|---|---|---|---|
| M18:orchestrator | `backend/sessions/manager.py`, `backend/ingestion/{mqtt_listener.py,service.py,store.py}`, `tests/{test_sessions.py,test_ingestion.py}`, PLAN/MEMORY/docs | Raw commit trước MQTT callback return; compute không chạy trên paho thread; START/STOP mẫu bay chờ boundary; fallback stop không bỏ raw pending; full pytest | done |

- `MqttListener` dùng một worker tuần tự cho compute. Callback MQTT gọi
  `IngestionService.accept_message()` để validate và commit raw trước, sau đó
  worker gọi `process_sample()`. Nếu process dừng giữa hai bước, startup
  `catch_up()` xử lý raw chưa có `processed_samples`.
- `SessionManager` đặt boundary gate trong lúc chờ ACK START/STOP. `assign()`
  chờ gate rồi mới truy vấn session, nên `session_id` của assessment, buffer
  predictor và event detector không bị tách ở mẫu ACK kế tiếp. Stop fallback nhìn
  cả raw đã commit, không chỉ `session_samples`.
- Regression test cạnh tranh giữ ACK rồi đưa mẫu qua START/STOP: mẫu không hoàn
  tất compute trước boundary và sau đó được ghi đồng nhất vào
  `processed_samples` + `session_samples`. Test listener xác nhận callback
  không bị giữ bởi compute worker.
- Verification: `.venv/bin/pytest tests/ -q` → **379 passed, 11 skipped**, 2
  dependency warnings; `git diff --check` PASS.
- Snapshot runtime ngày **2026-10-03**: **67.930 raw**, **67.930 processed**,
  **7 boot**, **0 session**, **20 invalid** (`ECHO_TIMEOUT` 14,
  `ECHO_STUCK_LOW` 3, `ECHO_STUCK_HIGH` 3), chưa có artifact trong
  `ai_model/artifacts/`, health đang `model_type=rf`, `model_available=false`.
  Khoảng sequence lớn ở boot `97a2ed799a12eec2` và
  `654d9761d552a47b` phải được cắt thành run riêng khi windowing; không gom
  toàn bộ raw thành một dataset giả.
- AI image sau rebuild source M18 chạy nhóm `tests/test_lstm_{train,inference,evaluation}.py`
  → **58 passed**, 42 dependency/deprecation warnings; không tạo artifact production.
- M18 không train/promote model từ dữ liệu chưa có session và không đổi `Sp`
  demo. Bước AI kế tiếp là flash/kiểm tra firmware mới, mở session thật theo
  từng scenario/target, hiệu chuẩn HC-SR04, export các session đã đóng, rồi
  train/evaluate LSTM cùng persistence/constant-velocity/RF trên split theo
  session.
