# PLAN — HRC Safety Log: TV3 + TV4 (RF baseline → LSTM)

Triển khai và xác minh phạm vi TV3 (ESP32, ingestion, session, hiệu chuẩn,
thuật toán ngưỡng) và TV4 (dataset theo session, LSTM dự báo, baseline,
đánh giá và runtime). Repository đã có baseline RF chạy được; kế hoạch này
không xóa RF ngay mà chuyển dần sang LSTM theo các mốc nhỏ, có rollback.
TV1–TV2 (Smart Contract, canonicalize, hash, dashboard và bộ gom sự kiện
chính thức) nằm ngoài phạm vi; TV3/TV4 chỉ bàn giao assessment/event package.

**Trạng thái kế hoạch: M1–M21 và các gate software/local của V2 đã hoàn tất. Release AI canonical runtime là `lstm-v1`; artifact/report cũ theo ngày đã được dọn khỏi runtime. Synthetic V2 đầy đủ, audit/hash/split, candidate evaluation 3 seed, local product E2E, chain-outage retry, blockchain local settlement và release bundle đã có evidence. Candidate `lstm-v2-synthetic` bị từ chối promotion vì constant-velocity thắng regression và false-warning rate của LSTM cao; runtime tiếp tục dùng `lstm-v1`. M4 firmware build PASS ESP-IDF 6.0.1 cho ESP32 và ESP32-S3; hardware flash, đo/hiệu chuẩn thật, dữ liệu thật bổ sung, soak trên bo và public/testnet deployment vẫn là gate mở. Phiên này triển khai/review/test trực tiếp, không dùng subagent.**

## 1. Kiến trúc

```
ESP32 + HC-SR04 ──MQTT QoS1──> Mosquitto ──> iot_backend/ingestion (validate → lưu raw SQLite → compute)
                                               ├─ iot_backend/safety   (Sp, CLEAR/VIOLATION/UNKNOWN)
                                                ├─ iot_backend/inference (LSTM hoặc RF legacy → 5 horizon)
                                                └─ processed_samples + event_packages (API bàn giao TV2)
ai_model/  : windows/features dùng chung train+runtime, prepare/split, scaler,
             baselines, train/evaluate LSTM, RF legacy adapter
scripts/   : session CLI, export, replay (offline), ingest JSONL, device simulator, synthetic dataset
```

Đơn vị nội bộ: **SI (m, s, m/s)**. Payload telemetry giữ mm + µs như spec. Chuyển đổi tại biên: `distance_m = distance_mm / 1000`.

## 2. Quy ước chung (bắt buộc mọi milestone)

- Python: venv `.venv/` (đã tạo, Python 3.12.3). Chạy từ repo root: `.venv/bin/python -m pytest tests/ -q`, `.venv/bin/python -m scripts.session_cli ...`, `.venv/bin/uvicorn iot_backend.main:app`.
- Import: package top-level `iot_backend`, `ai_model`, `scripts` (có `__init__.py`); không cài đặt, chạy từ repo root.
- Cấu hình qua env + `.env.example` (không bí mật): `MQTT_HOST=localhost`, `MQTT_PORT=1883`, `HRC_DB_PATH=data/hrc.db`, `DEVICE_ID=esp32_01`, `SAFETY_CONFIG=configs/safety.yaml`, `MODEL_CONFIG=configs/model.yaml`, `MODEL_ARTIFACT_DIR=ai_model/artifacts`. Nạp bằng `python-dotenv`.
- Topic: telemetry `hrc/v1/devices/{device_id}/telemetry` (QoS1, retain=false); control `hrc/v1/devices/{device_id}/control` (iot_backend→device); ack `hrc/v1/devices/{device_id}/ack` (device→iot_backend).
- Khóa mẫu: `(device_id, boot_id, sequence)`. Không dùng thứ tự nhận MQTT làm thứ tự đo.
- Schema JSON: `schemas/telemetry_v1.schema.json`, `schemas/assessment_v1.schema.json`, `schemas/control_v1.schema.json`, `schemas/ack_v1.schema.json`. Validation bằng `jsonschema` (Draft 2020-12).
- DB: SQLite WAL tại `HRC_DB_PATH`; DDL duy nhất trong `iot_backend/ingestion/schema.sql`; mọi truy cập qua `iot_backend/ingestion/store.py`.
- Dữ liệu/artifact không commit: `data/` (trừ `data/samples/` mẫu nhỏ), `ai_model/artifacts/`, `.venv/`, `sdkconfig`, `managed_components/`.
- Không tự giảm Sp; không biến UNKNOWN thành CLEAR; không bịa kết quả thực nghiệm — dữ liệu synthetic phải ghi nhãn `synthetic` rõ ràng.

## 2b. Baseline trước khi chuyển LSTM (lịch sử; trạng thái mới ở mục 6/8)

- M1–M6 đã có code iot_backend/safety, ingestion SQLite WAL, session, firmware
  ESP-IDF source, RF pipeline, runtime inference và 147 test pass tại thời điểm
  lập kế hoạch.
- Tại baseline M6, firmware ESP32/HC-SR04 chưa build/flash vì chưa có
  toolchain. Ngày 2026-10-02, M4f đã build PASS trên ESP-IDF 6.0.1 cho
  ESP32-S3; flash/chạy đo vẫn chưa kiểm chứng. Không ghi nhận hardware DoD
  hoàn thành chỉ từ log build.
- `.venv` iot_backend dùng Python 3.12.3, NumPy 2.5.3 và không cài TensorFlow.
  P0 đã kiểm chứng trong environment tạm: TensorFlow 2.20.0 + Keras 3.15.1
  tương thích thực tế, model `(10,2)→(5,)` fit/predict/save/load pass. Quyết
  định vận hành: dùng environment AI riêng (`tensorflow==2.20.0`) để không
  phình/can thiệp `.venv` iot_backend; chỉ đưa TensorFlow vào iot_backend runtime nếu
  M13 chứng minh cần thiết.
- Tại baseline M6, pipeline dùng `d0..d9`, `model.joblib`, `rf-v1`; chưa có tensor
  `(10, 2)`, scaler train-only, `model.keras`, metrics RMSE/F1/lead-time đầy đủ,
  hay runtime `predicted_violation_ms`.
- Chưa coi dữ liệu synthetic là dữ liệu đo thật. Calibration `Z_d`, event
  debounce/pre-post raw package và kiểm chứng broker/board sẽ là các gate riêng.

### Bảng DB (DDL chuẩn trong `iot_backend/ingestion/schema.sql`)

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

Quy tắc: `measured_at` tại lúc bắt đầu đo; chưa sync → `measured_at=null, time_synced=false` (không thay bằng giờ iot_backend). Timeout → `distance_mm=null, valid=false, error_code` thuộc enum `ECHO_TIMEOUT|ECHO_STUCK_LOW|ECHO_STUCK_HIGH|TRIG_FAIL`; `echo_us` giữ nếu đo được. Miền giá trị: `sequence>=1`, `monotonic_us>=0`, `distance_mm` 0..4500, `echo_us` 1..30000. Mẫu `valid=false` đúng schema vẫn lưu raw.

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

## 3. Thuật toán ngưỡng (iot_backend/safety)

Hàm thuần, SI: `Sh = vh*(Tr+Ts)`, `Sr = vr*Tr`, `Ss = vr*Ts/2`, `Sp = Sh+Sr+Ss+C+Zd+Zr`, `margin_m = distance_m - Sp`.

- `configs/safety.yaml`: danh sách phiên bản `config_version, valid_from (ISO-8601 UTC), params{vh, vr, tr_s, ts_s, c_m, zd_m, zr_m}, measurement_zone_m{min: 0.10, max: 1.00}`. Chọn config cho thời điểm `t`: phiên bản có `valid_from <= t` lớn nhất (mới nhất trong quá khứ, không phải mới nhất tuyệt đối).
- Trạng thái: mẫu hợp lệ + `margin_m > 0` → `CLEAR`; `margin_m <= 0` → `VIOLATION`; mẫu lỗi/thiếu → `UNKNOWN` (tách `sensor_valid` và `assessment_valid`).
- Cảnh báo cấu hình (không chặn, không giảm Sp): `sp_m <= measurement_zone_m.min` (không còn vùng quan sát trước ngưỡng) hoặc `sp_m > measurement_zone_m.max` (Sp ngoài vùng đã kiểm chứng) → thêm chuỗi vào `config_fit_warnings`.
- Fixture đối chiếu: vh=1, vr=0.2, Tr=0.1, Ts=0.2, C=0.05, Zd=0.03, Zr=0.02 → Sp = 0.44 m chính xác.

## 4. Dataset, model, inference (ai_model + iot_backend/inference)

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
  `CLEAR`. Model hỏng không được làm iot_backend/safety chết.
- Đánh giá bắt buộc MAE **và RMSE theo từng horizon** cho LSTM, persistence,
  constant-velocity; cảnh báo phải có precision, recall, F1, false-warning
  rate, miss rate và lead time (ms), tổng hợp và theo scenario/target_type.
  Không có event thì metric tương ứng là `null`, không bịa; kết quả LSTM thua
  baseline phải được báo cáo nguyên trạng.

## 5. Milestones đã hoàn tất

| ID | Nội dung | Files chính | Kiểm tra |
|----|----------|-------------|----------|
| M1 | Scaffold + schemas + configs + module safety + test safety | `requirements.txt`, `.env.example`, `configs/*`, `schemas/*`, `iot_backend/safety/*`, `tests/test_safety.py` | `pytest tests/test_safety.py -q` |
| M2 | Ingestion core: validate, store, dedup, gap/late, catch-up, ingest_jsonl + test | `iot_backend/ingestion/*`, `scripts/ingest_jsonl.py`, `tests/test_ingestion.py` | `pytest tests/test_ingestion.py -q` |
| M3 | Sessions (control MQTT + fallback), CLI, FastAPI, replay + test | `iot_backend/sessions/*`, `iot_backend/main.py`, `scripts/session_cli.py`, `scripts/replay.py`, `tests/test_sessions.py`, `tests/test_api.py` | `pytest tests/test_sessions.py tests/test_api.py -q` |
| M4 | Firmware ESP-IDF + Wi-Fi macro/mảng hai chiều | `firmware/esp32/**`, `tests/test_firmware_*.py` | M4j: build IDF 6.0.1 PASS ESP32/S3, 65 native firmware cases; bản sửa mới chưa flash/chạy đo |
| M5 | ai_model: windows/features, synthetic, split, baselines, train, evaluate + test | `ai_model/*`, `scripts/synthetic_dataset.py`, `tests/test_windows.py`, `tests/test_model.py`, `tests/test_events.py` | `pytest tests/test_windows.py tests/test_model.py tests/test_events.py -q` |
| M6 | iot_backend/inference + tích hợp compute + bàn giao TV2 + stale + test tích hợp | `iot_backend/inference/*`, sửa `iot_backend/ingestion/service.py`, `tests/test_inference.py`, `tests/test_integration.py` | `pytest tests/test_inference.py tests/test_integration.py -q` |
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
| M6 | done | M6a1 predictor (iot_backend/inference/predictor.py) + M6a2 test (10) + M6b tích hợp _compute_and_store + main.py + replay/ingest_jsonl --model-dir + test_integration (3) — 147 passed. Orchestrator tự chạy E2E độc lập: WARMUP/OK/VIOLATION+early_warning/replay offline idempotent đều đúng. sp None → inf (early_warning False, không bịa). Predictor CLI là instance riêng từng lần chạy |
| M7 | done (docs baseline) | README/docs wiring, data collection, TV2 handoff và experiments đã được cập nhật; `scripts/device_sim.py` là gap lúc M7, đã bổ sung ở M16 |
| P0 | done | Environment tạm TensorFlow 2.20.0/Keras 3.15.1 trên Python 3.12.3/NumPy 2.5.3; fit/predict/save/load smoke pass; không sửa repo; iot_backend 147 test pass |
| M8 | done (PASS WITH NOTES) | Config LSTM + RF legacy, assessment fields, model manifest và 20 contract tests; full suite 167 pass. Date-time `FormatChecker` chưa enforce vì env thiếu validator; các note còn lại chuyển sang M9/M11/M13 |
| M9 | done (PASS WITH NOTES) | LSTM feature API `(N,10,2)`, distance+delta, exact config contract, finite/malformed guards, RF không hồi quy; full suite 183 pass |
| M10 | done (PASS WITH NOTES) | Dataset JSONL/generic theo session, split trước windowing, scaler train-only và hash helpers; targeted 53 pass, full suite 208 pass, independent smoke 9/9. Hai minor fail-loud/documentation notes không chặn M11 |
| M11a | done (PASS WITH NOTES) | Training LSTM lazy-TensorFlow + 5 artifact (`model.keras`, `scaler.json`, `split_manifest.json`, `manifest.json`, `metrics.json`), schema/hash/shape loader và staging; iot_backend 214 passed/8 skipped, AI env 13 passed/1 skipped, smoke độc lập 36/36. Không commit artifact; còn minor metrics-integrity/zero-ratio/config-read/import-boundary notes |
| M12a | done (PASS WITH NOTES) | Metric primitives MAE/RMSE, grouped regression, extended event metrics (F1/FWR/miss/lead ms) với null/finite semantics; targeted 55 pass, full 236 pass/8 skipped; smoke độc lập 36/36. Ba minor test/documentation notes không chặn M12b |
| M12b | done (direct) | Lazy-import test, train/evaluate CLI, artifact metrics/hash/config gate, cùng test split RF/LSTM, MAE/RMSE/event/breakdown; AI thật pass |
| M13 | done (code) | MODEL_TYPE chọn LSTM/RF; shared preprocessing, unrounded threshold/horizon, fail-safe, session replay, restart prime; API + MQTT artifact thật pass |
| M14 | done (code) | Debounce 2/3, first assessment, state/event/processed atomic, deterministic ID, raw bracket 2s/1s + coverage; 10 event-package tests pass |
| M15 | done (tool) | Calibration bias/MAE/sample sigma/proposal Zd theo điều kiện; source explicit, không tự sửa config. Board calibration NOT VERIFIED |
| M16 | done | Simulator MQTT control/ack + labelled recording, closed-session export, requirements-ai và docs |
| V1 | software verified | Sau M4j: iot_backend 375 passed/11 skipped; AI Docker 58 targeted passed; full AI trước đó 321 passed/0 skipped. Self-review trực tiếp, hardware/real-data acceptance còn mở |
| M17 | done (Docker) | MQTT/iot_backend healthy, named-volume persistence, broker-only restart và 50 raw/boot + device_ack PASS; AI image integration 58 pass |
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
  khóa thực tế về MAE, trainer còn import `iot_backend.util`, và loader chưa hash/
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
| P0:env | Kiểm tra tương thích Python 3.12, NumPy hiện tại và TensorFlow/Keras; quyết định pin `requirements.txt` hoặc environment AI riêng; không phá `.venv` iot_backend | Có lệnh cài tái lập, import TensorFlow + train/predict smoke hoặc ghi rõ environment riêng; baseline `pytest tests/ -q` vẫn pass | — |
| M8:contract | Cập nhật `configs/model.yaml`, schema assessment (preprocessing/model metadata, `predicted_violation_ms`), model-version contract; giữ RF legacy rõ ràng | Schema validate cả assessment cũ và mới theo quyết định tương thích; config đủ window/features/LSTM/train params; test contract | P0 |
| M9:window | Chuyển representation LSTM sang `(10,2)` `[distance_m, delta_distance_m]`, `delta[0]=0`; giữ adapter RF d0..d9; sửa `ai_model/windows.py`, `features.py` và test | Không nối qua session/boot/gap/error; shape/order/delta đúng ở train-runtime; test window cũ không hồi quy | M8 |
| M10:data (DONE) | Hỗ trợ JSONL + export session/SQLite, split session trước window aggregation, scaler train-only, hash dataset/split; `ai_model/prepare.py` và module preprocessing mới | Không leakage; scaler không đọc val/test; dropped-window stats; `split_manifest.json` và SHA-256 tái lập; 208 test pass | M9 |
| M11a:train (DONE) | Bổ sung Keras LSTM baseline theo kiến trúc chốt; EarlyStopping, ReduceLR, checkpoint; lưu 5 artifact; giữ RF train path | Output `(N,5)`, artifact đủ file/field/version/hash, loader fail-loud, smoke cross-environment pass; 214 iot_backend tests + 36/36 independent checks | M10, P0 |
| M12a:metrics (DONE) | Thêm MAE/RMSE primitives, grouped helpers và event metrics mở rộng mà không phá API M5 | Shape/nonfinite guards, finite/null JSON semantics, F1/FWR/miss/lead ms; 55 targeted + 236 full pass | M11a |
| M12b:evaluate (DONE software) | Wiring evaluator LSTM/RF/persistence/constant-velocity trên cùng session test split; RMSE/MAE từng horizon, event metrics extended và breakdown scenario/target_type | Báo cáo test theo session; không tune test; metric `null` khi không xác định; không che trường hợp LSTM thua baseline; artifact LSTM chỉ đọc qua loader/hash gate | M12a, M11a |
| M13:runtime (DONE software) | Cập nhật `iot_backend/inference/predictor.py`, `iot_backend/main.py`, ingestion và scripts replay: buffer LSTM, preprocessing giống train, `predicted_violation_ms`, WARMUP/INVALID/MODEL_UNAVAILABLE; RF fallback tách biệt | 9→WARMUP, mẫu 10→OK, reset gap/error/boot/session, inverse scaling không round trước threshold, safety sống khi model hỏng | M11, M8 |
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
| M12b:orchestrator | ai_model/evaluate.py, ai_model/lstm_train.py, ai_model/train.py, ai_model/features.py, ai_model/prepare.py, ai_model/preprocessing.py, schemas/model_manifest_v1.schema.json, tests/test_lstm_*.py, tests/test_evaluation.py | Evaluator + CLI chung split, lazy import, hash/shape/finite gate; iot_backend pytest + AI env integration | done |
| M13:orchestrator | iot_backend/inference/*, iot_backend/settings.py, iot_backend/main.py, iot_backend/ingestion/{service.py,mqtt_listener.py}, scripts/replay.py, scripts/ingest_jsonl.py, .env.example, requirements-ai.txt, tests/test_lstm_inference.py | Preprocessing train/runtime giống nhau; reset boundary; fail-safe; API/replay test và artifact thật | done (software) |
| M14:orchestrator | iot_backend/events/*, iot_backend/ingestion/{schema.sql,store.py,service.py}, iot_backend/main.py, schemas/event_package_v1.schema.json, tests/test_event_packages.py | Debounce 2/3, sự kiện idempotent, pre 2s/post 1s cùng session/boot, persistence/recovery | done (software) |
| M15:orchestrator | scripts/calibrate.py, tests/test_calibration.py, docs/calibration.md | Bias/MAE/sigma/Zd, nhãn nguồn/điều kiện thử, không tự sửa safety config | done (software) |
| M16:orchestrator | README.md, docs/*, scripts/device_sim.py, scripts/export_dataset.py, tests/test_device_sim.py, tests/test_export_dataset.py, .gitignore | CLI/docs/simulator hoạt động, hardware gates rõ | done (software) |
| V1:orchestrator | PLAN.md, PROJECT_MEMORY.md; review các diff trên | Full suite iot_backend + AI, smoke offline/API, báo hardware/real-data/reconnect gate chưa kiểm chứng | done (software) |

### Evidence chốt ngày 2026-10-02

- `.venv/bin/python -m pytest tests/ -q`: **310 passed, 11 skipped**, 2 dependency
  deprecation warnings; skip chỉ các integration cần TensorFlow.
- `TF_CPP_MIN_LOG_LEVEL=3 TF_NUM_INTEROP_THREADS=1 TF_NUM_INTRAOP_THREADS=1
  OMP_NUM_THREADS=1 /tmp/blockchain-ai-env/bin/python -m pytest tests/ -q`:
  **321 passed, 0 skipped**, 41 dependency deprecation warnings (FastAPI
  TestClient/httpx và Keras/NumPy); không có test failure.
- Môi trường AI tái lập bằng `python3 -m venv /tmp/blockchain-ai-env` rồi pip
  `-r requirements-ai.txt`; `.venv` iot_backend không cài TensorFlow.
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
4. Docker persistence và iot_backend reconnect sau restart riêng broker đã PASS
   (M17). Reconnect khi mất mạng và soak trên ESP32 thật vẫn chưa kiểm chứng.
   M18 đã khóa race START/STOP bằng worker compute tuần tự và boundary gate;
   raw commit trước ACK, assessment/session/predictor/event dùng cùng session scope.
   DB thật hiện có 67.930 raw, 7 boot và 0 session — chưa thể train trước khi
   người vận hành mở/đóng các session có kịch bản và nhãn thật.
5. TV2 tiêu thụ/canonicalize/hash/gửi chain và dashboard ngoài phạm vi repo.

## 9. M4f — sửa build với ESP-IDF đã cài (2026-10-02)

| Task trực tiếp | Mục tiêu/phạm vi | Tiêu chí và evidence | Trạng thái |
|---|---|---|---|
| M4f:orchestrator | firmware/esp32/{CMakeLists.txt,sdkconfig.defaults,dependencies.lock,main/CMakeLists.txt,main/idf_component.yml,main/telemetry.c,.vscode/settings.json,README.md}, .gitignore và docs/plan/memory liên quan | `idf.py -C firmware/esp32 set-target esp32s3` + `build` exit 0; app/bootloader/partition size checks PASS; `esptool --port /dev/ttyACM0 chip-id` nhận diện ESP32-S3 revision v0.2 | done (build); flash/runtime chưa kiểm chứng |

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
| M4g:orchestrator | firmware/esp32/main/{wifi_mqtt,network_setup,network_console,diagnostics,hcsr04,telemetry,app_main}.*, main/{CMakeLists.txt,Kconfig.projbuild}, tests/firmware/**, tests/test_firmware_*.py, firmware README và docs liên quan | Scan/list, masked join, NVS commit sau IP, rollback join lỗi, retry, broker đổi runtime, sensor/enqueue fault/recovery; UART/USB receive tests; IDF build esp32s3 + esp32; full pytest | done (code/build/native); board acceptance mở |
| M17:orchestrator | Dockerfile, .dockerignore, compose.yaml, compose.env.example, docker/mosquitto/mosquitto.conf, .gitignore, README.md, docs/{docker,wiring,data-collection}.md, PLAN.md, PROJECT_MEMORY.md | Compose config/build/up PASS; mqtt/iot_backend healthy; telemetry/control/ack, SQLite + MQTT persistence và broker-only reconnect; AI TensorFlow integration | done (Docker); service tiếp tục chạy |

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
- ESP32-S3 build `firmware/esp32/build/hrc_esp32.bin`: **947680 bytes**, app
  1MiB còn10%; log `/tmp/blockchain-firmware-check/serial-setup-build.log`.
- ESP32 build riêng `/tmp/blockchain-firmware-esp32/build/hrc_esp32.bin`:
  **935776 bytes**, app còn11%; log `/tmp/blockchain-firmware-check/esp32-build.log`.
  Không thay sdkconfig/lock target S3 trong repo để kiểm tra ESP32.
- `esptool --port /dev/ttyUSB0 chip-id`: **ESP32-D0WD-V3 revision v3.1**,
  CP2102. Không flash binary S3 vào chip này. Chưa nạp bản sửa lên bo;
  đọc serial 3s không thấy output, không kết luận cảm biến/USB đã ổn.

### Docker và smoke thực tế

- Project `blockchain-hrc`, Mosquitto2.0.22 host1885→1883; iot_backend API8001→8000,
  SQLite volume `backend_data`. Giữ các service dự án khác trên1883/1884/8000.
  MQTT volume `mqtt_data`, configs/artifacts iot_backend chỉ đọc, restart/log rotation.
- Dockerfile target `iot_backend` không có TF; target/profile `ai` thêm TF2.20.0 /
  Keras3.15.1. Backend LSTM chọn target ai + MODEL_TYPE=lstm + subdir=lstm-v1.
  Không đưa artifact synthetic smoke vào runtime mặc định; /health hiện
  model_type=rf, model_available=false, safety vẫn tính.
- `docker compose config --quiet`, `build iot_backend`, `--profile ai build ai`,
  `up -d --wait mqtt iot_backend` PASS; hai container healthy. Backend và AI
  `python -m pip --no-cache-dir check` PASS.
- AI image chạy `tests/test_lstm_{train,inference,evaluation}.py` qua mount
  read-only: **58 passed**, gồm train/save/load/predict thật; 41 dependency
  warnings, không failure. `/tmp/blockchain-firmware-check/docker-ai-tests.log`.
- Smoke simulator `--device-id esp32_01`, host127.0.0.1:1885; API8001.
  **50/50 raw sequence1..50** mỗi boot, START/STOP device_ack, các lượt có
  20–21 session samples. Lượt đầu fail stats0 do simulator mặc định esp32_sim
  khác DEVICE_ID, không phải bằng chứng SQLite ingest hỏng.
- Restart mqtt+iot_backend giữ nguyên phiên SQLite 21 mẫu và retained test topic.
  Retained topic test đã clear. Restart **riêng mqtt** cũng PASS control/ack
  và 50 raw sau reconnect của iot_backend (không restart iot_backend để hồi phục).
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
- `git diff --check` PASS; private local header ignored; MQTT/iot_backend Docker
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
| M4i:orchestrator | firmware/esp32/main/{wifi_mqtt.c,wifi_mqtt.h,telemetry.c,Kconfig.projbuild}, tests/{test_firmware_network.py,test_firmware_telemetry.py,firmware/network_harness.c,firmware/telemetry_harness.c,firmware/stubs/idf_stub.h,firmware/stubs/freertos/{task.h,queue.h},firmware/stubs/esp_random.h}, firmware README, docs/wiring.md, PLAN.md, PROJECT_MEMORY.md | Repro red trên code cũ cho SDK mutex trong task đo và outbox vượt ngưỡng; zero-wait producer, bounded transfer/reserve, subscribe retry/SUBACK gate, PUBACK watchdog, ACK priority, log valid/invalid/disabled; full pytest; ESP32/S3 build; broker probe riêng | done (code/build/native); bản mới chưa flash/soak trên bo |

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
- Docker mqtt/iot_backend vẫn healthy, không restart; private header ignored,
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
| M4j:orchestrator | `firmware/esp32/{sdkconfig.defaults,sdkconfig}`, `main/{app_main.c,telemetry.c}`, PLAN/MEMORY | Option log mặc định 1000 ms; fallback khi sdkconfig cũ thiếu; log task startup; full pytest; build S3/ESP32 không warning/error | done (source/config/build); chưa flash bản M4j |

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
| M18:orchestrator | `iot_backend/sessions/manager.py`, `iot_backend/ingestion/{mqtt_listener.py,service.py,store.py}`, `tests/{test_sessions.py,test_ingestion.py}`, PLAN/MEMORY/docs | Raw commit trước MQTT callback return; compute không chạy trên paho thread; START/STOP mẫu bay chờ boundary; fallback stop không bỏ raw pending; full pytest | done |

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


## 15. M19 — runner thu dataset có tín hiệu START/STOP (2026-10-08)

**Trạng thái: done (code + tests). Người dùng duyệt triển khai trực tiếp,
không gọi agent. Lượt thu mới trên bo cần người vận hành thực hiện.**

Mục tiêu: chọn kịch bản cho từng lượt, gửi START/STOP xuống ESP32 qua iot_backend,
và xuất dữ liệu có nhãn mà không nhập curl hay gán nhãn lại raw chạy nền.

| Task trực tiếp | File | Tiêu chí | Trạng thái |
|---|---|---|---|
| M19:orchestrator | scripts/collect_dataset.py, configs/collection.example.yaml, tests/test_collect_dataset.py, docs/data-collection.md, README.md, docs/background-dataset-collection.md, PLAN.md, PROJECT_MEMORY.md | Runner HTTP/Enter hoặc duration, START/STOP qua iot_backend, export nhãn và manifest, delayed telemetry/timeout/Ctrl+C, loader/windowing compatibility | done (software); hardware run mở |

- Interactive mode: chọn scenario/target/tốc độ/ghi chú; Enter để START rồi
  Enter để STOP. Single-run CLI có các nhãn tương tự. YAML có defaults và
  repetitions; file mẫu 60 lượt, 30 s/lượt và dừng chờ người vận hành trước
  mỗi START. Non-interactive yêu cầu duration hữu hạn >0 ở mọi lượt.
- Runner dùng API8001, không mở Store hoặc MQTT listener riêng. Backend gửi
  session_start/session_stop QoS1 tới topic control; ACK ESP32 chốt boot/sequence.
  Firmware đo liên tục. Nhãn scenario/target lưu ở iot_backend; speed/ghi chú giữ
  trong manifest và speed_level trong từng dòng dataset.
- Mặc định yêu cầu START có device_ack; thiếu ACK thì đóng session vừa mở và
  hủy lượt trước khi báo thực hiện động tác. --allow-db-fallback cho phép thu
  nhưng đánh dấu WARN. API chỉ công khai nguồn START, nguồn STOP lưu
  not_exposed_by_api; không báo giả STOP đã nhận ACK.
- Export chờ đủ mọi sequence trong boundary, kể cả mẫu giữa tới sau STOP ACK,
  timeout hữu hạn mặc định 8 s. Kiểm tra JSON schema, nguồn boot, device,
  boundary, duplicate, invalid, boot_change và nhịp từ configs/model.yaml.
- Ghi raw export, JSONL từng session, dataset.jsonl của đúng các lượt runner
  đã thu và manifest atomic/SHA-256. Nhãn JSONL gồm session_label, scenario,
  target_type, speed_level, synthetic=false; không thay payload MQTT gốc.
  Schema/provenance sai được giữ raw nhưng không gắn nhãn real. PASS/WARN
  cùng có trong dataset tổng; phải xem manifest trước khi chọn dữ liệu train.
- Ghi trạng thái request + spec trước POST. Không replay POST sau timeout;
  manifest ghi needs_reconciliation để tra session bằng GET. Ctrl+C lúc
  thực hiện động tác đóng/xuất lượt đang sở hữu, đánh dấu interrupted.
- Mỗi lần chạy mặc định tạo output riêng; không ghi đè/nối vào thư mục đã có
  dữ liệu. Session ID chỉ ASCII an toàn, dài <=63 theo buffer firmware.
- Hardware smoke ngày 2026-10-08 xác nhận ACK START có thể đến sau 2 giây;
  trace broker cho thấy lệnh tới đúng topic nhưng ACK về muộn. Đã nâng mặc định
  `HRC_ACK_TIMEOUT_S` lên 8 giây trong Compose/iot_backend/docs và thêm test cấu
  hình; iot_backend runtime đã recreate với timeout 8 giây. Đây chưa phải bản sửa
  backlog: lượt thu tiếp và probe trực tiếp vẫn tái hiện ACK trễ hơn 8 giây
  (xem M4k). Chưa có bằng chứng quy lỗi này cho reconnect/control subscription.

Bằng chứng:

```text
.venv/bin/python -m pytest tests/test_collect_dataset.py -q
30 passed, 2 dependency deprecation warnings
.venv/bin/python -m pytest tests/ -q
409 passed, 11 skipped, 2 dependency deprecation warnings
.venv/bin/python -m scripts.collect_dataset --help
PASS
git diff --check
PASS
```

Integration test dùng HTTP adapter tới FastAPI endpoints thật, SessionManager
và MqttControlTransport thật; thiết bị ACK giả lập, payload/control schema thật.
Test giữ telemetry cuối đến sau STOP ACK và xác nhận nhãn/boundary đúng. Sáu
lượt cùng boot load được vào LSTM split theo session và không ghép window qua
lượt. Test không gửi control thật hoặc tạo session mới trong DB production.

Hardware smoke ngày 2026-10-08: một session `approach/person` nhận
`device_ack`, export 266 mẫu `quality=PASS`, sequence 5257..5522, không thiếu
sequence/invalid/timing error. Hai lượt sau tái hiện ACK muộn với timeout 2 giây;
trace xác nhận ACK về sau khi iot_backend đã fallback. Không tạo artifact/model
production và không commit. Checkpoint OpenCode là dữ liệu cũ; phiên Codex
không có tool checkpoint nên giữ nguyên file plugin runtime.

Next: người vận hành chạy runner, chọn target thực, thực hiện từng kịch bản,
kiểm tra manifest và bổ sung session đo độc lập trước train/evaluate.


## 16. M4k — ACK chậm sau telemetry backlog (2026-10-08)

**Trạng thái: done (source/native/build ESP32-S3 + ESP32). Kiểm chứng trên bo
sau flash còn mở. Tiếp tục sửa trực tiếp trong phạm vi lỗi thu nhiều lượt.**

| Task trực tiếp | File | Tiêu chí | Trạng thái |
|---|---|---|---|
| M4k:orchestrator | firmware/esp32/main/wifi_mqtt.c, tests/{test_firmware_network.py,firmware/network_harness.c}, scripts/collect_dataset.py, tests/test_collect_dataset.py, firmware README, docs/data-collection.md, PLAN/MEMORY | ACK vượt SDK FIFO; telemetry 10 Hz/6 lượt START/STOP; giữ QoS1/mailbox zero-wait/reserve; chỉ trở lại menu khi session lỗi đã xác nhận closed; full pytest/build S3/ESP32 | done (source/native/build S3+ESP32); flash/device acceptance mở |

- Lượt `run_20261008_053436_8c7658` PASS 321 mẫu, sequence20877..21197;
  lượt tiếp `run_20261008_053537_bc30a8` thất bại với timeout8s. Log bo:
  wifi=1/mqtt=1/control=1, pending16, outbox~23KiB, ESP_ERR_NO_MEM/drop.
- Probe MQTT trực tiếp (ID chẩn đoán riêng, không mở session iot_backend): broker
  PUBACK START1ms nhưng device ACK10.686s; STOP PUBACK42ms/device ACK5.2s.
  Assertion device ACK<8s FAIL. Không sửa/xóa dataset thật hoặc DB session.
- Source esp-mqtt1.0.0: enqueue nối FIFO QUEUED, không đánh thức poll; SDK
  gửi một queued message/vòng rồi poll receive (mặc định tới1s). Ưu tiên RAM
  trước đây không vượt được telemetry đã nằm trong FIFO SDK. Mô hình drain
  chậm bắt được START quá8s và ACK bị chặn sau80 packet; cả hai test RED
  trước sửa. Chưa xác định đầy đủ độ trễ TCP/radio thực trên bo.
- Worker gọi esp_mqtt_client_publish trực tiếp cho telemetry và ACK; ACK được
  chọn trước mỗi lần gửi, không chờ FIFO telemetry cũ. Giữ QoS1/outbox,
  zero-wait producer/mailbox16+4, byte reserve, SUBACK gate và PUBACK watchdog.
  Không gọi SDK/socket từ task đo hoặc callback MQTT.
- Menu collector tiếp tục sau CollectionError chỉ khi API đã xác nhận STOP
  closed cho session đang sở hữu. Timeout START/STOP vẫn thoát, giữ
  needs_reconciliation và không retry POST. Regression RED→GREEN cho lỗi
  thiếu START ACK rồi lượt kế tiếp PASS; test timeout START/cleanup STOP giữ
  đúng một POST mỗi loại và không mở thêm lượt.
- Validation: full pytest415 passed/11 skipped/2 dependency warnings;
  firmware network/Wi-Fi/telemetry targeted43 passed. Native regression mô
  phỏng6 lượt30s cùng telemetry10Hz, đủ2106 telemetry và12 ACK, không drop/
  backlog/reconnect. Đây là kiểm chứng mô phỏng, chưa phải kết quả radio thật.
- ESP-IDF6.0.1 build S3 PASS: binary0xe8870, app partition còn9%; SHA256
  acf09a7918776c72524c9520a19d7418a82b2ac6178bcc1c6f9c94a75da02d3b.
  ESP32 PASS trong /tmp/hrc-mqtt-esp32-build, binary0xe59f0/còn10%.
  ESP32 configure có2 CMake warning sinh ROM gdbinit do thiếu
  ESP_ROM_ELF_DIR; không có compiler warning/error. Restore lock targetS3,
  không đổi sdkconfig/credentials, không flash và không commit.
- Hardware còn mở: monitor người dùng đang giữ /dev/ttyACM0. Cần thoát monitor,
  flash binary S3 mới rồi đo lại latency START/STOP và thu ít nhất5 lượt30s.
  Một lượt PASS không đủ nghiệm thu; không tăng timeout để che backlog.


## Migration cấu trúc repo — 2026-10-08

- Đổi package Python `backend` thành `iot_backend`, `iot_code` thành `firmware`.
- Hợp nhất source repo clone vào `blockchain_api`, `smart_contracts`, `dashboard`; docs tại `docs/blockchain`.
- Repo clone cũ và cache firmware được chuyển ra `/tmp/blockchain-layout-migration-20261008`; Git cha giữ nguyên.
- Compose service/target `iot_backend`; volume SQLite giữ tên vật lý `blockchain-hrc_backend_data`.
- Kiểm tra: Python 415 passed/11 skipped; Node 46 passed; Hardhat compile PASS, 49 passed; Compose config PASS.
- Hai pipeline IoT và blockchain chưa tích hợp tự động; không commit/push trong migration.

## R1 — Train/evaluate dataset real và xuất hình (2026-10-09)

**Trạng thái: done cho xử lý dữ liệu, train và báo cáo offline.** Người dùng
đồng ý giữ số đo thực tế dù các session gần cảm biến nằm dưới Sp, và yêu cầu
preprocess/train/đánh giá F1 rồi tạo ảnh. Thực hiện trực tiếp trong Codex.

| Task | Phạm vi file | Tiêu chí | Evidence |
|---|---|---|---|
| R1:orchestrator | scripts/{prepare_real_dataset,evaluate_all_horizons,export_ai_predictions,plot_ai_results}.py, tests/test_prepare_real_dataset.py, PLAN.md, PROJECT_MEMORY.md; data/processed, data/results, ai_model/artifacts (ignored) | Giữ telemetry và invalid thật; chia session/scaler train-only; train LSTM/RF; evaluate test + baseline; PNG/CSV/PDF có support và N/A đúng | Dataset 8.062 rows/22 session; 7.716 windows; bundle data/results/real-20261009/README.md và verification.json |

- Dataset SHA256 `6680a9e34c5daa1d3066c7d7431f1e31c2fe07f2d498354fb1b51ead5ba8edf4`.
  15 file real gồm pilot lịch sử và collection 08/09-10; 10 invalid giữ trong
  raw, cắt window; không gap/jitter. Kiểm trùng sample key và phép đo theo
  device/timestamp/nội dung; đối soát mọi telemetry field với nguồn PASS.
  Nhãn mdeium đã được sửa trong JSONL nguồn; 5 manifest speed lệch JSONL được
  ghi audit, lấy label từng dòng nhất quán, giữ file nguồn.
- Seed42 mặc định: train/val/test 16/3/3 session; 5812/878/1026 windows.
  LSTM21 epochs, best11. Test MAE mean14.954688mm/RMSE mean27.797813mm.
  RF15.933874/30.961428mm; persistence13.996101/30.503347mm;
  constant-velocity17.850921/37.240893mm. Test0crossing nên F1/recall N/A.
- Seed18 là thử nghiệm **thăm dò**, chọn sau kiểm tra event coverage, không
  thay kết quả seed42. Train/val/test5517/1027/1172 windows; LSTM39epochs,
  best29. Test3crossing, LSTM MAE18.529942mm/RMSE40.494399mm. LSTM/RF có
  3TPepisode/0FP, precision=recall=F1offline1.0, lead~100ms trên 100–500ms.
  Baseline không cảnh báo: recall0, precision/F1N/A theo evaluator.
- Đây là F1 trước debounce. Đã chạy `EventDetector` thật trong SQLite
  :memory: với dự báo test, stale=false: seed18 LSTM/RF3raw warning points
  → **0PREDICTED_WARNING confirmed** do history2/3 và CLEAR chỉ1mẫu trước
  crossing. Không trình bày F1offline1.0 như khả năng cảnh báo live.
- Artifact riêng: ai_model/artifacts/{lstm-real-20261009,rf-real-20261009,
  lstm-real-event-20261009,rf-real-event-20261009}; TF2.20/Keras3.15.1CPU.
  Model loader/schema/hash, shape(10,2)→(5), scaler train-only, split
  disjoint đã xác minh; MAE/RMSE tính lại từ predictions.csv khớp report.
- Bundle data/results/real-20261009: dataset/config snapshots, audit,
  models/test predictions/JSON, 5CSV, 20PNG300dpi/20SVG, 2PDF10trang;
  ZIP data/results/real-20261009-results.zip. Matplotlib3.11.2 ở /tmp,
  giữ môi trường backend không TF; không deploy runtime hoặc commit.
- Verification: merger6passed; preprocessing/evaluator101passed/1skip ở
  host; nhóm RF/data89passed; TFartifact/evaluator42passed trong Docker
  (Keras NumPy deprecation warnings). `git diff --check` PASS.
- Giới hạn: một thiết bị/các session cùng buổi, windows chồng lấn, target là
  số đo HC-SR04 không phải khoảng cách chuẩn, chỉ3testevents; sensor_check
  nằm train. Dữ liệu gần vẫn dùng theo yêu cầu người dùng. Báo cáo ghi rõ
  split18 exploratory, signal spikes/coverage và tác động debounce.

Next: dùng bundle làm kết quả thực nghiệm offline; nếu cần đánh giá vận hành,
thu test sessions có đoạn CLEAR đủ dài/nhãn khoảng cách tham chiếu và replay
runtime đã debounce. Checkpoint OpenCode cũ giữ nguyên vì Codex không có tool
checkpoint; không tạo ID hoặc cập nhật task state giả.

## Chọn LSTM runtime và dataset bổ sung — 2026-10-09

- Theo yêu cầu người dùng, runtime mặc định chuyển sang LSTM artifact
  `lstm-real-20261009`; Docker target mặc định là `ai`, env mẫu và README/docs
  không còn chọn RF. Hai thư mục artifact RF real đã xóa; RF source và metric
  lịch sử trong bundle giữ lại để truy xuất đối chứng, không được nạp runtime.
- Recreate Docker AI ban đầu gặp bind cổng 8001 còn do container cũ; sau dừng
  container cũ và nối container mới vào network `blockchain-hrc_default`, API
  khỏe. `GET /health`: `status=ok`, `mqtt=true`, `model_type=lstm`,
  `model_available=true`. Artifact loader pass; inference smoke 10 mẫu trả
  `OK`, đủ 5 horizon. SQLite volume không bị xóa.
- Đây là runtime thử nghiệm/báo cáo, chưa chứng nhận an toàn: test seed42 không
  có crossing; seed18 exploratory có 3 crossing nhưng detector debounce xác
  nhận 0 `PREDICTED_WARNING`. Safety status vẫn là lớp độc lập; model lỗi không
  tự fallback sang RF.
- `docs/data-collection.md` thêm ma trận test mới: 20 approach crossing độc lập,
  10 stop-before-Sp, 10 stationary-clear, 10 oscillation quanh Sp, 5 recede,
  và tối thiểu 5 lượt cho từng mã sensor error quan sát được. Giữ nguyên các
  session test ở một ngày/buổi khác, có ít nhất 20 crossing và 20 session âm,
  đo ground truth độc lập; fault test tách khỏi F1 crossing vì LSTM hiện không
  phân loại lỗi sensor.


## M20 — Thưởng/phạt session và LSTM trên ESP32-S3 (2026-10-09)

**Trạng thái: đang triển khai; người dùng đã yêu cầu triển khai và flash trực tiếp.**
Tiếp tục phần code đã bàn giao, giữ dữ liệu và thay đổi có sẵn trong workspace.

| Task | Phạm vi | Tiêu chí | Trạng thái |
|---|---|---|---|
| M20:orchestrator-firmware | firmware/esp32/main/{lstm_inference.c,lstm_inference.h,telemetry.c,Kconfig.projbuild}, tests/firmware/lstm_harness.c, tests/test_firmware_lstm.py, scripts/generate_esp32_lstm.py | Trọng số/scaler đúng artifact; parity Keras; invalid/gap/session reset; build S3, flash ACM0, serial LSTM OK | software done; flash pending |
| M20:orchestrator-policy | iot_backend/{rewards,safety,sessions,main.py}, configs/safety-square.yaml, tests/test_rewards_policy.py, tests/test_safety.py | Boundary mô phỏng 40 mm; lịch sử 440 mm giữ nguyên; đủ raw/processed/boundary trước thưởng; lỗi không phạt, vi phạm không bị xóa | done (policy/API) |
| M20:orchestrator-chain | smart_contracts/contracts/HRC{RewardToken,SessionRewards}.sol, smart_contracts/test/HRCReward.js, smart_contracts/scripts/deploy_reward.js, blockchain_api/{rewardService,rewardPolicy,server}.js, scripts/session reward runner + tests | Ký quỹ 2, thưởng 3, phạt 2; quỹ 1000; 1 lần/ví/ngày; idempotent API; deploy/smoke local và kết nối session IoT thật | done local; public-chain deploy pending |
| M20:orchestrator-report | scripts/verify_esp32_lstm.py, data/results/esp32-lstm-20261009 (ignored), docs/blockchain/session-rewards.md, firmware README, README, PLAN/MEMORY | PNG/SVG/PDF dự báo/parity/metric/coin từ kết quả thật, provenance + các giới hạn | done local evidence; hardware flash pending |

Checkpoint OpenCode trong repo là lịch sử cũ; phiên Codex hiện không có tool
checkpoint. Theo dõi công việc này tại PLAN/MEMORY và artifacts kiểm chứng,
không tạo session ID hoặc evidence plugin giả.


## R2 — Chốt LSTM-only và đánh giá dataset real 2026-10-10

**Trạng thái: done cho xử lý offline và đồng bộ runtime phần mềm.**

- Dataset canonical: `data/processed/real-20261010/dataset.jsonl`, 12.820 mẫu /
  49 session; dataset train LSTM: `lstm-dataset.jsonl`, 12.361 mẫu / 47 session.
- Session split cố định trước windowing: train/validation/test = 33/7/7 session,
  7.046/2.740/1.902 windows. `sensor_check` được giữ riêng trong
  `sensor-fault-dataset.jsonl`.
- Ngưỡng đánh giá bộ dữ liệu mới: `Sp = 0.04 m = 40 mm`. Tập test có 82 crossing
  xuống/chạm Sp, nên event metrics có support.
- Model triển khai duy nhất là LSTM. Persistence và constant-velocity chỉ là
  baseline offline. CLI evaluator chỉ nhận `--model-type lstm`; RF trainer,
  artifact/report RF và đường dẫn runtime cũ đã được loại khỏi active pipeline.
- Kết quả 500 ms: LSTM MAE 6,46 mm, RMSE 10,68 mm; precision N/A, recall 0,
  F1 0, miss rate 1. F1 bằng 0 vì test có event nhưng LSTM không tạo warning
  episode nào; không trình bày MAE thấp như bằng chứng cảnh báo an toàn.
- Artifact active: `ai_model/artifacts/lstm-real-20261010/`. Firmware weight/scaler
  đã regenerate theo model/scaler hash trong artifact; native parity test dùng
  vector của artifact này.
- Report/ảnh active: `data/results/lstm-only-20261010/`; gồm JSON, CSV, PNG, SVG
  và PDF. Artifact/report cũ ngày 09/10 và RF report không còn trong thư mục active.
- Verification: `.venv/bin/python -m pytest -q` = 417 passed, 12 skipped, 2 warnings;
  Docker image `blockchain-hrc:ai` build PASS; `/health` runtime trả
  `model_type=lstm`, `model_available=true`. Flash/soak trên board thật vẫn là
  gate phần cứng riêng.


## R3 — Train/test lại synthetic qua pipeline LSTM chuẩn — 2026-10-10 (done)

Người dùng đã yêu cầu train/test lại; thực hiện trực tiếp trong Codex.

| Task | Phạm vi file | Tiêu chí và kiểm tra |
|---|---|---|
| R3:orchestrator | PLAN.md, PROJECT_MEMORY.md, docs/synthetic-square40-benchmark.md; artifact/report benchmark (ignored) | Config chuẩn; split/scaler train-only; test + detector 2/3; ảnh/bundle có provenance; verifier, pytest synthetic và diff-check PASS |

- Đã dùng `data/synthetic/square-40mm-20261010/dataset.jsonl` với `synthetic=true`,
  split cố định 280/60/60 session và hash
  `0823c297f9248cce2dcb49dab682a9c24ce19a6aebda9e84f3838485de67d6d8`.
- Đã train bằng đúng `configs/model.yaml` vào artifact riêng
  `ai_model/artifacts/lstm-benchmark-square40-20261010`; không ghi đè artifact
  real/runtime. Scaler fit train-only; test có 13.560 windows. Train 11 epoch;
  EarlyStopping lấy checkpoint val_loss tốt nhất ở epoch 1, giữ nguyên đường
  học và giới hạn validation trong README.
- Kết quả test horizon 500 ms: MAE trung bình 2,56 mm, RMSE trung bình 3,24 mm;
  51 crossing; Precision 1,000, Recall 0,882, F1 0,9375 trước debounce.
  Replay detector 2/3: Precision 1,000, Recall 0,647, F1 0,7857, miss rate
  0,3529, lead time trung bình 124,2 ms.
- Đã xuất `evaluation-500ms.json`, `evaluation-all-horizons.json`,
  `evaluation-debounced.json`, `predictions.csv`, `verification.json`,
  README, 10 PNG + 10 SVG + PDF và ZIP report trong
  `data/results/lstm-benchmark-square40-20261010/`.
- Kiểm tra: `tests/test_synthetic_square_dataset.py` 8 passed; verifier xác nhận
  synthetic labels, dataset/model hash, split không chồng session, metric tính
  lại từ CSV và 30 file real/runtime không đổi; `git diff --check` PASS.
- Giới hạn báo cáo: đây là benchmark synthetic chạy qua pipeline như dữ liệu đo,
  nhưng không được trình bày là phép đo ESP32/HC-SR04 hay bằng chứng an toàn
  phần cứng. Kết quả real vẫn ở `data/results/lstm-only-20261010/`.


## M20 — Bằng chứng triển khai local blockchain + edge AI (2026-10-10)

**Trạng thái: software deployment PASS; flash phần cứng chưa thực hiện vì không có cổng serial.**

- Hardhat node đang chạy tại `http://127.0.0.1:8545`, chain ID `31337`. Đã redeploy sạch:
  `HRCSafetyLog=0x5FbDB2315678afecb367f032d93F642f64180aa3`,
  `HRCRewardToken=0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512`,
  `HRCSessionRewards=0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0`.
- Contract tests: **64 passing**. Blockchain API tests: **49 passing**.
- Reward smoke với beneficiary khác penalty treasury: `REWARDED`, `REWARD_DAILY_LIMIT`,
  `PENALTY`, `NO_REWARD`, duplicate retry idempotent; số dư beneficiary `20 → 21 HRC`,
  quỹ `1000 → 997 HRC`. Evidence: `data/results/edge-blockchain-20261010/reward-smoke.json`.
- Blockchain API live chạy tại `http://127.0.0.1:3000`; `GET /` pass và `POST /api/session-outcome`
  settle thành công qua HTTP với transaction ở block 17. Evidence nằm trong
  `data/results/edge-blockchain-20261010/api-*.json`.
- Runtime IoT/edge vẫn chỉ dùng artifact LSTM thật `ai_model/artifacts/lstm-real-20261010`.
  SHA-256 model `6713054121907c92645924d7e34a13db10867d19871fa161fe747f1fab1a9f0b`,
  scaler `164c42206f8214f0be20d8788b018a2fcb46c0ef132b3dc53f19b2ff43457734`, threshold
  `Sp=40 mm`, input `(10,2)`, output `(5,)`.
- Native parity: **7 passed, 1 skipped**; generator `--check` pass. ESP-IDF **6.0.1**
  target ESP32-S3 build pass, binary `firmware/esp32/build/hrc_esp32.bin`, app còn 7%.
- Không thấy `/dev/ttyACM*` hoặc `/dev/ttyUSB*` trong lần triển khai; chưa claim flash,
  serial LSTM, HC-SR04 wiring, Wi-Fi/MQTT soak hay session real-to-chain tự động.
- Hình reward và report máy đọc: `data/results/edge-blockchain-20261010/reward-policy.{png,svg}`
  và `data/results/edge-blockchain-20261010/deployment-report.json`.

Lệnh xác minh chính:

```bash
cd smart_contracts && npm test
cd ../blockchain_api && npm test
cd .. && .venv/bin/python -m pytest tests/test_firmware_lstm.py -q
cd firmware/esp32 && source /home/lucas/.espressif/v6.0.1/esp-idf/export.sh && idf.py build
```


## M21 — Hoàn thiện demo IoT → reward → dashboard (2026-10-10)

**Đã hoàn tất và được kiểm chứng trực tiếp ngày 2026-10-10. Checkpoint OpenCode không khả dụng trong phiên này.**

- `M21:orchestrator-api`: API đọc cấu hình deployment, HRC balance/pool, session và reward transactions; start idempotent bằng operator, giữ Bearer auth cho write.
- `M21:orchestrator-bridge`: enrolment các session mới, đóng session → chờ raw/processed coverage → hash evidence → settlement; lưu outbox và retry cùng payload sau restart. Không tự stake/settle dữ liệu lịch sử.
- `M21:orchestrator-dashboard`: giao diện Session Rewards, địa chỉ chain động, live distance/LSTM, trạng thái pending/error, refresh tự động; không đưa key/token vào browser.
- `M21:orchestrator-verify`: regression Python/Node/Hardhat, smoke HTTP/MQTT local có provenance, screenshot PNG, lệnh demo trong docs, cập nhật PLAN/MEMORY; bổ sung test durable outbox cho lỗi/retry/idempotency.

Tiêu chí: start giữ 2 HRC trước session; clear đủ coverage trả stake và xét 3 HRC/ngày; vi phạm valid mất 2; fault/incomplete trả stake; retry không trùng thanh toán; dashboard đọc số dư, evidence, tx/block đúng chain 31337. LSTM runtime real giữ nguyên. **M21: done.**

## V2 — AI → blockchain → dashboard product pass (2026-10-10)

**Trạng thái: triển khai trực tiếp, software verification pass; các gate model/browser/hardware còn mở.**

- `M22/M23`: **done (software)**. Đã sinh release đầy đủ 1.200 forecast + 150 fault session, 270.000 samples, audit/hash/split pass; telemetry runtime và ground truth tách riêng. Evidence: `data/results/square-bench-v2/audit.json`, prepared manifest và `scripts/audit_synthetic_v2.py`.
- `M25/M26`: runtime model metadata, edge prediction schema/store/parity, source provenance, CORS allowlist, SSE/polling live snapshot và violation confirmation 2-of-3. `OBSERVED` không tạo chain outbox; `CONFIRMED` mới đủ điều kiện reward/chain.
- `M27/M28`: chain event outbox/bridge retry cùng payload, xử lý `202 pending`, dead-letter cho 4xx, public deployment config và reward policy dùng confirmed violation.
- `M24`: **done (evaluation decision, không promote)**. Candidate `lstm-v2-synthetic` đã train/evaluate trên frozen test 33.480 windows, 3 seed; xuất all-horizon regression, baseline, episode precision/recall/F1/lead time và scenario breakdown. Kết luận promotion `REJECTED_SYNTHETIC_GATE`; giữ active `lstm-v1`.
- `M29`: **done (software/browser smoke)**. Dashboard đã nối `/api/config`, `/live/latest`, `/live/history`, `/system/health`, SSE reconnect/polling fallback, SVG actual/threshold/forecast chart, source/model/provenance/stale state và dynamic wallet config. Chrome headless browser smoke/responsive screenshot pass ở 1440x1000 và 390x844.
- `M30`: **done (local software evidence)**. `tests/test_product_e2e.py` bao phủ session → telemetry → confirmed event → chain outage/retry cùng payload → reward retry cùng evidence → duplicate readback. Release bundle checksums nằm dưới `release/square-bench-v2-20261011/` (ignored). Public/testnet, hardware và production gates vẫn mở.
- Live performance sau index migration trên database hơn 1,1 triệu raw rows: `/live/latest` và `/system/health` p50/p95 khoảng 1–2 ms; `/live/history?limit=600` khoảng 23/56 ms. Compose source label hiện tường minh qua `HRC_SOURCE_MODE`, demo mặc định là `simulator` và vẫn hiển thị `threshold_verified_on_hardware=false`.

Evidence ngày 2026-10-10:

- `.venv/bin/python -m pytest -q`: **441 passed, 12 skipped, 2 warnings**.
- `npm --prefix blockchain_api test`: **53 passed**.
- `docker compose config`: **PASS**.
- `git diff --check`, `node --check dashboard/app.js`, `node --check dashboard/wallet.js`, `python -m py_compile scripts/synthetic_v2.py`: **PASS**.
- Browser smoke: Chrome headless rendered live dashboard with actual distance, Sp and five forecasts; desktop/mobile screenshots verified. Empty-chart overlay hidden state regression fixed.

Không commit `.opencode/state/`; không đưa private key/bearer token vào dashboard.

## R5 — Hoàn tất V2 software/local gates và cập nhật release plan — 2026-10-11

**Trạng thái: software/local evidence PASS; model promotion và hardware/public-chain gates được quyết định rõ, không giả lập.**

### M22/M23 — Release dataset V2

- Generator: `scripts.synthetic_v2` với seed `20261010`, đúng 1.200 forecast và
  150 fault sessions, mỗi session 20 giây ở 10 Hz.
- Dataset hash: `56fd67c14a155361f37f7e90f5e21b3341e7fc8463d193c4b57043818440c87c`.
- Telemetry hash: `de0bfdf4f258811b2b70241948de8a532c25bc2016ca25fac357ade57837c069`.
- Audit `data/results/square-bench-v2/audit.json`: **PASS**; session/seed disjoint,
  ground truth không lọt vào `telemetry.jsonl`, hash bytes khớp manifest.
- Derived views: `data/synthetic/square-bench-v2-prepared/` gồm
  `training_dataset.jsonl` 240.000 samples và `fault_dataset.jsonl` 30.000
  samples; fault sessions không đi vào train.

### M24 — Candidate AI V2

- Config mới: `configs/model-synthetic-v2.yaml`; artifact candidate:
  `ai_model/artifacts/lstm-v2-synthetic/`.
- Frozen split: 840/180/180 sessions, 156.240/33.480/33.480 windows.
- Ba seed `20261010/20261011/20261012` giữ cùng test split; report all-horizon
  ở `data/results/square-bench-v2/evaluation-*.json`, stability summary ở
  `data/results/square-bench-v2/stability-summary.json`.
- Quyết định: **REJECTED_SYNTHETIC_GATE**. Constant-velocity có MAE/RMSE thấp
  hơn trên frozen test; LSTM có recall tốt hơn ở horizon dài nhưng false-warning
  rate cao. Không ghi đè `lstm-v1`, không gọi candidate là production-safe.

### M30 — E2E, outage và release bundle

- `tests/test_product_e2e.py`: **PASS**; local safety vẫn lưu raw/assessment/event
  trong khi chain/reward outage, retry dùng payload/evidence bất biến và settle
  duplicate không tạo thanh toán thứ hai.
- Bundle: `release/square-bench-v2-20261011/`, có release manifest, checksums,
  dataset/model/evaluation/deployment evidence và runbooks. Bundle không chứa
  private key, token hoặc toàn bộ dataset 270.000 dòng.
- Local Docker MQTT/iot_backend vẫn healthy và runtime active identity giữ
  `model_version=lstm-v1`, `threshold_verified_on_hardware=false`; lần triển khai
  cuối bật cả reward bridge và chain bridge trên Hardhat local qua
  `docker compose --env-file compose.env` với token chỉ truyền qua environment.

### Gates còn mở

- ESP32 flash, HC-SR04 measurement, calibration, radio/reconnect và soak thật.
- Public/testnet deployment: chưa claim vì phiên này chưa có RPC/funded signer/
  confirmed receipts cho network công khai.
- Production safety certification, secure boot/flash encryption và HIL acceptance.

## R6 — Chốt triển khai local và cập nhật evidence — 2026-10-11

**Trạng thái: local deployment PASS; không còn việc software/local nào đang treo trong plan.**

- Runtime cuối cùng: `blockchain-hrc:ai` có TensorFlow `2.20.0`, Keras
  `3.15.1`; `/health` trả `model_available=true`, MQTT healthy, reward bridge
  và chain bridge `configured=true/running=true`.
- `/models/active` xác nhận `lstm-v1`, input `[10, 2]`, output `[5]`, đúng model
  SHA-256 `94d017fa94ca25ee22bacba740ac37102bd1537c82b009a61817cb62506b2b52`,
  `training_data=synthetic-only`, `simulation_mode=true` và
  `threshold_verified_on_hardware=false`.
- Blockchain local xác nhận Hardhat `chainId=31337`, safety/reward deployment
  cùng manifest, reward policy và API config cùng địa chỉ. Reward smoke đã có
  `REWARDED`, daily limit, `PENALTY`, `NO_REWARD` và duplicate retry idempotent;
  HTTP session settlement/readback có evidence trong bundle.
- Kiểm tra cuối: host `444 passed, 12 skipped, 2 warnings`; blockchain API
  `53 passed`; smart contracts `64 passing`; synthetic V2 audit `PASS`;
  `docker compose config` `PASS`; release bundle checksum `25/25` `OK`.
- `scripts/build_release_bundle.py` đã bổ sung evidence reward/API settlement
  local và runbook triển khai phân biệt local Hardhat với public/testnet.
- Release bundle mới nhất: `release/square-bench-v2-20261011/`, 24 file evidence
  và `checksums.sha256`; bundle không chứa private key, bearer token hay toàn bộ
  dataset generated.

### Việc không thể đánh dấu done từ workspace này

- Flash/chạy firmware ESP32, đo HC-SR04, calibration, Wi-Fi/MQTT reconnect và
  soak/HIL trên bo thật.
- Public/testnet deployment có RPC, funded signer, confirmations và receipts.
- Production safety certification, secure boot/flash encryption và compliance.

## R4 — Dọn artifact cũ, chốt release AI canonical và retry triển khai — 2026-10-11

**Trạng thái: software release và local deployment PASS; hardware gate vẫn mở.**

Người dùng yêu cầu xóa các thư mục model/result cũ, train lại gọn và triển khai.
Đã chọn một release duy nhất để tránh runtime trỏ nhầm artifact theo ngày:

```text
ai_model/artifacts/lstm-v1/
data/results/lstm-v1/
```

- Đã xóa toàn bộ artifact AI cũ dưới `ai_model/artifacts/`, các report AI
  `lstm-real-*`, `lstm-only-*`, `lstm-benchmark-*` và ZIP tương ứng. Giữ
  `data/synthetic/square-40mm-20261010/` làm dataset nguồn và giữ
  `data/results/edge-blockchain-20261010/` vì đây là evidence blockchain,
  không phải report model cũ.
- Đã train lại bằng TensorFlow `2.20.0`, Keras `3.15.1`, seed `20261010`,
  đúng `configs/model.yaml`, split theo session trước windowing và scaler
  train-only. Artifact gồm đúng năm file: `model.keras`, `scaler.json`,
  `split_manifest.json`, `manifest.json`, `metrics.json`.
- Manifest: input `(10,2)`, output `(5,)`, horizons `100/200/300/400/500 ms`,
  train/val/test windows `63280/13560/13560`; model SHA-256
  `94d017fa94ca25ee22bacba740ac37102bd1537c82b009a61817cb62506b2b52`,
  scaler SHA-256
  `20281417e61b5847f61413afa12245284ac88e6ae16b0d1c12b01da53e03986d`,
  dataset SHA-256
  `0823c297f9248cce2dcb49dab682a9c24ce19a6aebda9e84f3838485de67d6d8`.
- Report canonical có đánh giá từng horizon, persistence/constant-velocity
  baseline, predictions CSV, figures PNG/SVG/PDF, verification và replay
  detector 2/3. Horizon 500 ms: LSTM MAE trung bình `2.56 mm`, RMSE trung
  bình `3.24 mm`, F1 offline `0.9375`; sau debounce 2/3: precision `1.0`,
  recall `0.6471`, F1 `0.7857`, miss rate `0.3529`, lead time trung bình
  `124.2 ms`. Đây là synthetic-only benchmark, không phải chứng nhận an toàn.
- Đã regenerate `firmware/esp32/main/lstm_inference.c` từ artifact canonical
  và cập nhật native reference/hash tests. Chưa claim flash, đo HC-SR04,
  calibration, reconnect hoặc soak trên phần cứng.
- Đã đồng bộ `.env.example`, `compose.env.example`, `compose.yaml`, settings,
  README và docs để runtime mặc định dùng `MODEL_TYPE=lstm` và
  `MODEL_ARTIFACT_DIR=.../lstm-v1`. `scripts/verify_synthetic_benchmark.py`
  replay truyền `measured_at` từ source để kiểm tra timestamp đúng.
- Bằng chứng verifier: synthetic labels, dataset/model hash, split không chồng
  session và metric tính lại từ CSV đều PASS. `git diff --check` PASS.

Triển khai retry cần rebuild image để không dùng source/image cũ, recreate
`mqtt` và `iot_backend`, sau đó kiểm tra `/health`, `/models/active`,
`/live/latest`, native/runtime tests và full suite. Không commit
`.opencode/state/`, secret hoặc artifact generated bị ignore.

### R4 evidence chốt

- `docker compose build iot_backend ai`: PASS.
- `docker compose up -d --force-recreate --wait mqtt iot_backend`: PASS sau
  khi tăng healthcheck grace period lên 60 giây; cả MQTT và backend healthy.
- `/health`: `status=ok`, `mqtt=true`, `model_type=lstm`,
  `model_available=true`.
- `/models/active`: `ACTIVE`, `model_version=lstm-v1`, đúng model/scaler/dataset
  hash; artifact path `/app/ai_model/artifacts/lstm-v1`.
- `/live/latest`: prediction `OK`, đủ năm horizon, provenance trỏ `lstm-v1`,
  source `simulator`, `threshold_verified_on_hardware=false`.
- Host full suite: `441 passed, 12 skipped, 2 warnings`.
- Docker full suite: `380 passed, 73 skipped, 41 warnings`.
- Targeted firmware/runtime: `11 passed, 1 skipped`.
- Synthetic verifier trong image mới: PASS; `51` events, debounce precision
  `1.0`, recall `0.6471`, F1 `0.7857`.
- `docker compose config` và `git diff --check`: PASS.

## R7 — Pull-based deployment và dashboard qua Compose — 2026-10-11

**Trạng thái: hoàn thành ở mức source/Compose; registry publish vẫn cần tag và quyền ghi thực tế.**

- `compose.yaml` nhận `HRC_IMAGE`, hỗ trợ máy đích pull image AI rồi chạy với
  `--no-build`; service `dashboard` dùng Nginx ở cổng `8080`.
- Runtime không còn mount thư mục artifact host đè lên image; `.dockerignore`
  giữ canonical `ai_model/artifacts/lstm-v1/` trong image portable.
- `README.md` đã chuyển luồng máy mới thành: `docker pull` → `compose pull/up`
  → mở dashboard → kiểm tra `/live/latest` và MQTT telemetry thật từ ESP32.
- Compose config PASS; image kiểm tra local có đủ `model.keras` và `manifest.json`
  trong `/app/ai_model/artifacts/lstm-v1`.
- Blockchain API/Hardhat chưa được đưa vào image IoT; README ghi rõ đây là
  thành phần bổ sung nếu cần các tab Blockchain/Session Rewards.

## R8 — Fix dashboard LAN live-data access — 2026-10-11

**Trạng thái: hoàn thành.**

- Dashboard Nginx proxy cùng origin `/api/iot/*` tới `iot_backend:8000`, gồm cả
  SSE `/api/iot/live/stream`; browser không còn phụ thuộc vào CORS allowlist
  theo IP LAN.
- Frontend mặc định dùng proxy cùng origin, nhưng vẫn giữ
  `HRC_IOT_API_URL` override cho môi trường phát triển đặc biệt.
- Compose và `compose.env.example` dùng `HRC_SOURCE_MODE=esp32` mặc định cho
  deployment thực; demo simulator vẫn chọn explicit trong `compose.env`.
- Artifact canonical `ai_model/artifacts/lstm-v1` được track riêng trong Git;
  model candidate/report khác vẫn bị ignore, nên clone mới có thể build image
  portable mà không cần bind mount model từ host.
- Đã tái kiểm tra trường hợp thực tế: backend có telemetry/model nhưng browser
  ở `http://192.168.2.34:8080` không nhận được dữ liệu vì thiếu CORS origin.

## R9 — Wallet application identity, behavior tracking, and customer visibility — 2026-10-11

**Trạng thái: software và local deployment PASS; MetaMask extension signing thật vẫn cần kiểm tra trên trình duyệt người dùng.**

Mục tiêu của milestone này là nối wallet MetaMask với một application user riêng,
gửi behavior telemetry có idempotency và retry, rồi hiển thị User Portal cùng
Customer Directory có phân quyền. Blockchain ingest/admin bearer tokens vẫn tách
khỏi app session của người dùng.

Phạm vi hoàn tất:

- EIP-191 challenge/login theo chain, session app hash-only và wallet/user binding.
- Behavior queue phía dashboard không mất event khi API tạm lỗi, không gán event
  của ví cũ cho ví mới; backend chống duplicate và payload conflict.
- Customer store atomic, có stale-lock recovery sau process crash.
- `/api/me`, `/api/behavior`, `/api/me/behavior`, `/api/customers` và customer
  activity routes; User Portal/Customer Directory trên dashboard.
- Reward read path ưu tiên wallet application hiện tại để customer thấy đúng dữ liệu.

Tiêu chí kiểm tra: toàn bộ test `blockchain_api`, test persistence/reopen và
idempotency/identity isolation, `node --check` frontend/backend, `git diff --check`,
và smoke test HTTP wallet challenge → login → behavior → user/customer reads.
Deployment local chỉ được báo PASS khi API khởi động được với cấu hình rõ ràng;
public/testnet deployment và MetaMask extension signing thật vẫn là giới hạn môi
trường.


### R9 evidence chốt

- `npm --prefix blockchain_api test`: **58 passed, 0 failed**; gồm HTTP wallet
  challenge/login thật bằng ethers, concurrent challenge, idempotency conflict,
  session logout, inactive account, reopen và stale-lock recovery.
- `npm --prefix smart_contracts test`: **64 passing**.
- `node --check` cho customer store/service/API và dashboard app/wallet: PASS;
  `git diff --check`: PASS.
- Local HTTP smoke: challenge `200` → wallet login `200` → behavior `200` →
  `/api/me` đúng user → `/api/me/behavior` count tăng → admin `/api/customers`
  trả directory. Record ví ngẫu nhiên dùng cho smoke đã được dọn; store runtime
  hiện là snapshot rỗng và API đang giữ lock độc quyền.
- Local deployment đang chạy: dashboard `8080`, blockchain API `3000`, Hardhat
  JSON-RPC `8545`, chain `31337`; `/api/config`, `/api/events` và reward config
  đều trả `success=true`. Safety contract và reward contracts đã deploy lại từ
  scripts local.
- Chrome CDP smoke tại `http://127.0.0.1:8080/#user`: User Portal visible, activity
  table render, không có lỗi JavaScript; chưa mô phỏng được MetaMask extension ký
  message trong headless browser.

## R10 — Compact demo account picker and user reward transaction UX — 2026-10-11

**Trạng thái: hoàn thành implementation và kiểm chứng desktop/mobile/local chain.**

- Header chỉ còn nút `DEMO ACCOUNT`; modal nhỏ chọn `admin`, `user_1`, `user_2`,
  có Open, Cancel, close/overlay/Escape và quản lý focus. Open bị khóa cho tới
  khi chọn account; lỗi đăng nhập hiển thị ngay trong modal.
- `My Wallet / My Sessions` nằm ngay đầu Dashboard và xuất hiện trong Reports:
  wallet address, HRC balance, allowance, Approve 2 HRC, Start Session, receipt
  hash/block/event và kết quả settlement. Không thêm tab wallet/customer.
- Policy cạnh nút giao dịch: khóa 2 HRC; valid hoàn 2 + thưởng tối đa 3 HRC/ví/ngày
  UTC khi pool đủ; violation thu stake 2 HRC; fault/incomplete hoàn stake không
  thưởng. Chỉ operator gửi outcome/evidence; contract tính payout.
- My Sessions và Transaction history đọc state/event thật; Approval được lọc theo
  owner/spender, reward events theo beneficiary. Admin chỉ có báo cáo tổng hợp
  và chi tiết hai user, không có nút giao dịch.
- Ngăn double submit, serialize writes mỗi user, giữ pending reference qua reload
  để retry cùng session cả khi allowance đã tiêu. Refresh sau write chờ read cũ
  rồi đọc mới; response account cũ không ghi đè account hiện tại. Login hết hạn
  xóa dữ liệu cũ và yêu cầu chọn lại. UI poll settlement mỗi 5 giây.
- Mapping ví cố định sau restart API, private key giữ backend và signer chỉ cho
  chain `31337`. Giữ chain/deployment hiện có, không reset hoặc xóa session cũ.
- Session từ Dashboard cần operator gắn với safety/simulation evidence đúng ID;
  chưa xác minh tự gán user session vào luồng robot vật lý.

Kiểm tra cuối: blockchain API **71 passed** (gồm 7 dashboard regression và 4 demo
wallet service tests); smart contracts **64 passing**; JavaScript syntax, HTML
unique IDs/account form/bốn nav, `git diff --check` PASS. Chrome kiểm tra chọn
account bằng input thật, Open, refresh, user isolation, admin read-only, Reports,
desktop 1440×1000 và mobile 390×844; không JS exception hoặc overflow.

`scripts/verify_demo_wallet_ui.mjs` mặc định không gửi giao dịch ví.
Luồng `--transact` đã PASS trên Hardhat: Approval, stake đúng 2, retry không stake
thêm, user bị từ chối outcome, reward 3 và daily limit, penalty 2, fault/incomplete
refund, settlement retry, receipt/event/history/balance và tự cập nhật UI. Outcome
được mô phỏng có nhãn và gắn từng session mới; không phải kiểm chứng robot thật.
Evidence và screenshots: `data/results/demo-wallet-ui-20261011/`, gồm
`browser-result.json`, `browser-transactions-result.json`, `settlement-1..5.json`.
README/integration docs mô tả luồng chooser và cách chạy verifier.


## R11 — User sensor sessions, settlement and wallet ledger — 2026-10-11

**Implementation, full Docker build/deployment và kiểm chứng chain test + ESP32 PASS. Gate settlement trên RPC gốc còn mở vì node 8545 đã dừng; không đánh dấu toàn bộ runtime DoD hoàn tất.**

- R11:coder — sửa account picker chỉ có admin/user_1/user_2; thêm locked balance,
  End Session và ledger mở sẵn, phân biệt approve/stake/refund/reward/penalty.
- Backend demo điều phối Start → IoT session cùng ID → stake → End → outcome từ
  mẫu thật → operator settlement; lưu tiến trình/evidence/signed transactions,
  serialize thiết bị/signer, phục hồi retry và restart không stake lần hai.
- Chỉ chủ phiên được End; admin read-only; frontend không gửi outcome/payout.
- Khôi phục các stake cũ không có phiên IoT bằng INCOMPLETE, evidence rõ lý do,
  hoàn stake và giữ toàn bộ lịch sử; không reset/redeploy chain đang chạy.
- Phạm vi: blockchain_api demo/store/transaction/IoT client/routes và tests,
  dashboard HTML/JS/CSS, verifier/browser và integration tests/contracts,
  IoT outcome/ingestion/store và API regression cho lỗi replay toàn bộ lịch sử;
  Dockerfile chuẩn: build đầy đủ image AI/runtime theo yêu cầu mới của người dùng;
  README/integration docs/PLAN/PROJECT_MEMORY. Không sửa firmware/global config.
- DoD: API/contract/IoT regression PASS; browser account/log/mobile PASS; live
  ESP32 Start/End cùng ID, outcome khớp mẫu thật, receipt/balance/ledger đúng.
  Các kịch bản tự động chạy môi trường test riêng, không lấp dữ liệu robot giả.


### R11 — Evidence cuối và giới hạn runtime

- `R11:coder` đã triển khai và tự review diff trong Codex: picker ba account,
  wallet/locked balance/End/ledger; lifecycle sensor, signed transaction durable,
  operator-only outcome, retry/restart; mặc định `HRC_APP_AUTH_MODE=demo` khóa
  đăng ký ví EIP-191 trước khi tạo customer. API restart giữ đúng hai địa chỉ ví.
- Đã sửa outcome timeout do replay toàn bộ khoảng 1,4 triệu raw rows: chỉ catch-up
  các row còn pending trong boundary của phiên. Regression RED→GREEN; outcome
  phiên cũ 329 mẫu trả valid trong 12 ms trên image đầy đủ.
- Đã sửa cached sensor busy sau End và message cũ sau reload/RPC recovery;
  regression RED→GREEN. Browser verifier bật CDP Page trước khi inject API URL
  và kiểm tra browser/receipt reader dùng cùng API; chờ paint trước screenshot.
- Người dùng yêu cầu build đầy đủ: Dockerfile chuẩn từ `python:3.12-slim-bookworm`,
  cài toàn bộ requirements/AI dependencies, rồi COPY source/model canonical.
  Tách layer dependency để cache source-independent; có build args tùy chọn cho
  wheel cache. Gói TF 2.20.0 620,7 MB cache trên host đã đối chiếu SHA-256 PyPI
  `25265b0bc527e0d54b1e9cc60c44a24f44a809fe27666b905f0466471f9c52ec`.
  Đây là package cache cho full install, không dùng runtime image cũ làm FROM.
- Full Compose build/deploy PASS, image
  `sha256:0710e46941e8f04bf4cc42f6ecc9f2c1c0fb0762832f224a940340bdebfed5cc`.
  MQTT/IoT/dashboard healthy; pip check PASS; TF 2.20.0/Keras 3.15.1; model
  `lstm-v1` đúng hash; source ESP32; source files trong image khớp host; volume
  `blockchain-hrc_backend_data` và phiên sensor cũ được giữ nguyên.
- Test cuối: `npm test --prefix blockchain_api` **93 passed**;
  `npm test --prefix smart_contracts` **73 passing**. Trong image đầy đủ,
  `tests/test_sessions.py test_rewards_policy.py test_reward_bridge.py test_api.py
  test_ingestion.py test_product_e2e.py` **61 passed**, một warning deprecation
  Starlette/httpx có sẵn; compose config, syntax và diff check PASS.
- `smart_contracts/scripts/verify_live_wallet.js --network hardhat` PASS:
  contract test thật trong process riêng + API ESP32 thật + click Chromium
  desktop/mobile. Phiên `demo-user_2-live-71d41107-3f13-4578-8985-93713dbb7ddd`
  có 179 mẫu/41,303 giây, outcome INCOMPLETE/sequence_gap; số dư 20→18→20,
  hoàn 2 HRC, reward/penalty 0, settlement block 10/hash
  `0x2a2ecf55960fbd3de1c56d5ba1b55bc56588baa76bc00bc3a6a86198780f7cc3`.
  Browser tiếp tục lại chính phiên đã settled: không stake/settle thêm, số dư
  giữ nguyên. Không gán outcome giả để có reward. Policy valid/violation/fault/
  daily cap được test bằng sensor fixtures ghi rõ simulated trên chain riêng.
- Evidence tại `data/results/live-wallet-sessions-20261011/`: logs test,
  `api-restart-verification.json`, `image-deployment-final.json`,
  `isolated-chain/{live-sensor-settlement,verification-state}.json`, screenshot,
  `isolated-chain/resume/`. Không lưu token/private key/raw signed bytes trong
  evidence công khai. Store/backup riêng mode600 vẫn ignored.
- RPC gốc `127.0.0.1:8545` hiện không listen; chưa tìm thấy snapshot chain để
  restore. Giữ manifest và store gốc; API trả `LOCAL_CHAIN_UNAVAILABLE`, khóa
  writes, không tự reset/redeploy. Phiên gốc
  `demo-user_2-live-2fa2a61a-c366-40b1-9ed2-70a9c449c1a5` vẫn
  SETTLEMENT_PENDING với valid evidence 329 mẫu/33,334 giây. Kiểm chứng isolated
  không hoàn thành settlement của phiên đó. Recovery cũ đã có 13 receipt hoàn
  26 HRC trước khi RPC mất; không coi đây là số dư live sau khi node dừng.
- Next: khôi phục state/node gốc để worker settlement cùng evidence; nếu không
  có snapshot thì cần quyết định rõ về chain demo mới. Không làm mất lịch sử
  cũ hay biến một chain test thành deployment gốc. Checkpoint OpenCode không
  khả dụng trong Codex; file checkpoint lịch sử giữ nguyên, không tự tạo done.


## R12 — Deterministic local bootstrap and IoT/chain boundary fix — 2026-10-11

**Implementation and local chain smoke complete. Full Docker runtime remains to
be exercised on a machine with Docker Compose.**

- `scripts/run_local_demo.sh` now starts a fresh Hardhat chain, deploys safety
  and reward contracts into a per-run ignored directory, configures isolated
  stores, starts blockchain_api and optionally starts the Compose stack. The
  `--smoke` mode submits and confirms a real WARNING transaction, reads reward
  configuration and reads the seeded `user_1` wallet before clean shutdown.
- Local deployment manifests are generated runtime state. The stale tracked
  reward manifest was removed; deployment scripts accept `HRC_DEPLOYMENT_DIR`.
- Blockchain configuration uses `HRC_BLOCKCHAIN_NETWORK`; unrelated host
  `NETWORK` variables cannot change the selected chain. Local account 0 is
  derived automatically, while non-local networks still require `PRIVATE_KEY`.
- IoT outbox maps `PREDICTED_WARNING` to canonical chain state `WARNING`, keeps
  `sourceEventType`, omits absent optional values and omits a null session ID.
- Dashboard uses same-origin Nginx proxies for both IoT and blockchain APIs.
- Verification: Python **446 passed, 12 skipped**; blockchain API **98 passed**;
  smart contracts **73 passing**; two clean local smoke runs passed, including
  one with `NETWORK=caas_ingress_only`. Compose YAML, JavaScript/Python syntax
  and `git diff --check` passed. Docker CLI was unavailable in this executor,
  so the full container startup was not run here.

## R13 — Dashboard HTML/API response fix — 2026-10-11

**Implementation and non-Docker verification complete.**

- Root cause: a stale Nginx process or the documented Python static server can
  answer `/api/chain/*` with HTML. The dashboard previously passed that body to
  `response.json()`, producing `Unexpected token '<'` and an unusable wallet.
- The hosted dashboard keeps the same-origin `/api/chain` proxy. When that
  default route returns HTML or the proxy-specific unavailable code, local
  static development retries the blockchain API at the current host on port
  3000. Explicit `HRC_CHAIN_API_URL` values are never overridden.
- Nginx now returns structured JSON for unavailable IoT/blockchain upstreams.
  `run_local_demo.sh` parses and validates proxy JSON before printing `ready`.
- Verification: blockchain API **103 passed**; dashboard fallback tests include
  concurrent wallet/session/transaction reads; JavaScript and shell syntax,
  `git diff --check`, and a clean real-chain `--smoke` run passed. Docker/Nginx
  runtime validation remains for the user's Docker machine because this
  executor has no Docker CLI.

## R15 — Remove empty duplicated Safety Events surface — 2026-10-11

- **Done:** removed the legacy empty `/api/events` navigation/table/card/modal
  from the dashboard. Live telemetry remains under `Live Monitoring`; wallet,
  session and transaction evidence remain under Dashboard/Reports.
- **Verification:** `npm --prefix blockchain_api test -- --runInBand` → 103/103;
  `node --check dashboard/app.js`; `git diff --check`; browser verifier with
  `HRC_CHAIN_API_URL=http://127.0.0.1:3000` passed desktop/mobile, no JS errors,
  no horizontal overflow and no read-only wallet writes.

## R16 — Blockchain re-audit — 2026-10-11

- **Done:** verified deployment wiring, the pool/escrow invariant, the on-chain
  duration-bonus curve, and all four settlement outcomes plus idempotent retry
  against the live local stack. No contract or policy change was required.
- **Evidence:** `npx hardhat test` 74 passing, `npm --prefix blockchain_api test`
  103/103, `scripts/smoke_reward_service.js` all outcomes as expected, invariant
  `balanceOf(rewards) == rewardPoolBalance + escrowedStakeTotal` = true.

## R17 — Wallet-first migration — 2026-10-12

Đề xuất chuyển sang mô hình wallet-first: người dùng tự giữ ví, backend giữ vai
trò oracle/operator xác nhận dữ liệu cảm biến. **Người dùng đã duyệt P1–P7; kết quả triển khai ở mục D.**

### A. Đối chiếu đề xuất với code hiện tại

Đã có sẵn (không phải việc mới):

| Hạng mục đề xuất | Thực tế trong repo |
| --- | --- |
| Cờ `HRC_APP_AUTH_MODE` | Đã có, giá trị `demo` (mặc định) / `wallet`; route ví bị chặn 403 khi ở mode `demo` (`server.js`) |
| Đăng nhập ví EIP-191 + nonce + TTL + session | `customerService.js`: `createChallenge` / `authenticateWallet`, nonce 24 byte, TTL 300s, session 86400s |
| 4 endpoint auth | `/api/auth/challenge`, `/api/auth/wallet`, `/api/auth/logout`, `/api/me` |
| Owner 2 bước + role operator + event role | `pendingOwner`/`acceptOwnership`, `operators`, `OperatorAdded/Removed`, `CannotRemoveOwner` |
| Session gắn chủ sở hữu | `liveSessionService.owner()` trả `SESSION_OWNER_MISMATCH` |
| Bắt buộc thời lượng tối thiểu on-chain | `MIN_REWARD_DURATION = 30s`, bonus cap `MAX_REWARD_BONUS_PERCENT = 100` |
| Kế toán pool/escrow | `rewardPoolBalance` + `escrowedStakeTotal`, invariant đã audit (R16) |
| Chain chỉ lưu address/sessionId/outcome/amount/evidenceHash/timestamp | Đúng như `SessionSettled`; không có tên/email on-chain |

Delta thật cần làm:

1. **SIWE (EIP-4361)** — hiện message là chuỗi tự chế, chưa phải SIWE chuẩn.
2. **Bỏ hình dạng demo trong session ID** — `owner()` còn ép prefix `demo-<userId>-`.
3. **OpenZeppelin** — contract không import OZ (`Ownable2Step`, `AccessControl`,
   `ReentrancyGuard`, `Pausable` đang viết tay).
4. **Pausable + trần reward pool** — chưa có `pause()` và chưa giới hạn `fundRewardPool`.
5. **Tham số hóa policy** — `MIN_REWARD_DURATION`/bonus đang là `constant`.
6. **DB thay JSON** — `customerStore.js`/`demoSessionStore.js` là JSON + single-writer lock.
7. **Rút tiền / penalty treasury / slashing operator** — chưa có luồng rút.
8. **Admin API + audit log** — chưa có.
9. **UI wallet-first** — dashboard vẫn dùng account picker demo.

### B. Rủi ro phải chốt trước khi làm

- Đổi contract ⇒ **phải redeploy**, sinh địa chỉ mới, cập nhật
  `deployments/reward-localhost.json`, `compose.env`, artifact ABI và test.
- Bỏ `demo` mode ⇒ vỡ `run_local_demo.sh`, browser verifier và kịch bản demo
  đang dùng hằng ngày. Đề xuất **giữ song song** hai mode.
- Đổi format session ID ⇒ dữ liệu `.runtime/**/demo-sessions.json` cũ không đọc được.
- Chuyển JSON → DB ⇒ phải có migration + backup, ảnh hưởng docker volume.

### C. Thứ tự đề xuất (từng phase nhỏ, có rollback)

- **P1** SIWE sau cờ `HRC_APP_AUTH_MODE=siwe`, giữ EIP-191 cũ để tương thích.
- **P2** Bỏ ràng buộc prefix session ID, giữ đọc dữ liệu cũ.
- **P3** OZ refactor + `Pausable` + trần pool; redeploy local, cập nhật ABI/env.
- **P4** Tham số hóa policy on-chain (owner cấu hình minDuration/maxBonus).
- **P5** DB (SQLite trước, Postgres sau) + migration từ JSON.
- **P6** Admin API + audit log + luồng rút tiền treasury.
- **P7** Dashboard wallet-first (connect ví, ký SIWE), giữ demo như chế độ phụ.

### D. Trạng thái triển khai (đã xong)

- **P1** ✅ SIWE sau cờ `HRC_APP_AUTH_MODE=siwe` (`POST /api/auth/challenge` +
  `POST /api/auth/wallet`), EIP-191 cũ giữ nguyên để tương thích.
- **P2** ✅ Bỏ ràng buộc prefix session ID; vẫn đọc được dữ liệu cũ.
- **P3** ✅ OZ refactor + `Pausable` + trần `maxRewardPool`; contract đã redeploy
  local (`0x0DCd1Bf9A1b36cE34237eEaFef220932846BCD82`).
- **P4** ✅ Tham số hóa policy on-chain: `MIN_REWARD_DURATION=30`,
  `FULL_BONUS_DURATION=60`, `MAX_REWARD_BONUS_PERCENT=100` + `setPolicy()`.
  Thưởng scale tuyến tính: 30s→3 HRC, 45s→4.5 HRC, ≥60s→6 HRC (chạm trần +100%).
- **P5** ✅ SQLite store sau cờ `HRC_APP_STORE=sqlite` + script migration JSON→SQLite.
- **P6** ✅ Admin API + audit log (`GET /api/admin/audit`) + luồng rút treasury
  (`sweepExcessToken`, `setPenaltyTreasury`).
- **P7** ✅ Dashboard wallet-first (connect ví, ký SIWE); demo giữ làm chế độ phụ.

**Sự cố đã sửa trong quá trình xác minh:** `RewardService.getOperatorStatus()`
và `getAdminStatus()` không set `success: true`, khiến route admin trả **503**
dù payload hợp lệ (test cũ dùng fake service luôn có `success` nên che lỗi).
Đã sửa và thêm regression test `blockchain_api/tests/rewardServiceStatus.test.js`.

Trạng thái: **P1–P7 đã triển khai và xác minh trên local stack.**


## R18 — Sửa kết nối ví dashboard — 2026-10-12

Đã được người dùng yêu cầu tiếp tục triển khai; không cần duyệt lại plan R17.

| Task | Phạm vi | Tiêu chí / xác minh | Trạng thái |
| --- | --- | --- | --- |
| R18:orchestrator | dashboard/{app.js,wallet.js,index.html,style.css}, blockchain_api/tests/{wallet.test.js,dashboardAuthMode.test.js,demoDashboard.test.js}, scripts/verify_wallet_first_ui.mjs, docs/blockchain/integration.md, PLAN/MEMORY | Không ví: lỗi ngay cạnh CTA; ví inject muộn kết nối được; sai mạng và hủy ký có nút retry; SIWE thật qua local API; demo regression PASS; browser desktop/mobile PASS | done |

Checkpoint tool của OpenCode không có trong phiên Codex này; đã đọc checkpoint
local để tham khảo, không thay đổi con trỏ session OpenCode.

R18 evidence: `node scripts/verify_wallet_first_ui.mjs` PASS 7 tình huống trên
Chromium với SIWE signature và local API thật; không broadcast giao dịch HRC.
JSON/ảnh nằm trong `data/results/wallet-first-connect/` (ignored). Tests hồi quy
bổ sung: ví inject muộn, thiếu provider, thông báo gần CTA, retry sau hủy ký/sai
mạng, nhãn header được giữ sau refresh. Không reset chain hay xóa volumes.

## R19 — Khôi phục local RPC và Add Network flow — 2026-10-12

| Hạng mục | Phạm vi | Trạng thái |
| --- | --- | --- |
| Local chain recovery | Khởi động Hardhat `31337` trên `127.0.0.1:8545`, redeploy safety/reward contracts, nối API `3000` bằng đúng deployment manifest | done |
| MetaMask network onboarding | `wallet_switchEthereumChain` bắt lỗi `4902`, gọi `wallet_addEthereumChain` với HRC Local rồi retry switch | done |
| Browser verifier | Fake provider mô phỏng Add Network; cleanup Chrome profile có retry; real dashboard/API/SIWE verifier | done |

Evidence runtime hiện tại: dashboard `http://localhost:8080`, API `3000`, RPC
`8545`, chain `31337`; verifier PASS 7 scenario, gồm
`wrong-network-add-chain`. Ví MetaMask mới chưa tự có ETH gas/HRC; cần faucet
local hoặc cấp test funds tới đúng địa chỉ trước khi chạy Approve/Start.

## R20 — Reconcile wallet-first Start với sensor session — 2026-10-12

| Hạng mục | Phạm vi | Trạng thái |
| --- | --- | --- |
| Post-sign reconciliation | `confirm-start` xác minh session on-chain, lưu durable record và mở ESP32 | done |
| Dashboard action state | Sau MetaMask broadcast, gọi reconcile để hiển thị `End Session` | done |
| Orphan recovery | Hai session thử nghiệm trước patch được settle `INCOMPLETE`, hoàn lại 4 HRC | done |

Evidence: blockchain API `132/132` pass; wallet test có coverage session
reconcile; ví test hiện `20 HRC`, khoảng `1 ETH`, escrowed stake `0 HRC`.


## R21 — Wallet End lifecycle và đồng hồ session — 2026-10-12

Người dùng yêu cầu sửa End bị lỗi và thêm thời gian chạy; tiếp tục trong phạm vi
đã được duyệt. Làm trực tiếp trong phiên Codex, không dùng subagent. Đã đọc state
OpenCode local; checkpoint tool không khả dụng, không sửa con trỏ workflow cũ.

| Task | Mục tiêu / phạm vi sửa | Tiêu chí và lệnh kiểm tra | Trạng thái |
| --- | --- | --- | --- |
| R21:orchestrator | server End route; dashboard app/index/style; walletFirst/demoDashboard tests; browser verifier; PLAN/MEMORY | POST End gọi live lifecycle có owner/empty-body/idempotence guards; timer HH:MM:SS tick mỗi giây và freeze theo sensor boundary; `npm --prefix blockchain_api test`, browser desktop/mobile, runtime End | done |

Regression trước sửa: 21 pass / 4 fail ở walletFirst + demoDashboard. End nhận
HTTP 200 nhưng không settled; body outcome/reward giả cũng được bỏ qua. Sau nối
route vào LiveSessionService.end và thêm timer: API suite 135/135 pass.

Runtime End evidence: phiên `hrc-e9a65572-demo-dbf840be-4b1b-4979-8ea8-ef9e358b4aa1`
được xử lý theo yêu cầu End của người dùng bằng cùng LiveSessionService.end sau
khi dừng API cũ (không có hai writer). Sensor `closed`, duration 726.856s, 7266
samples, outcome `VIOLATION_CONFIRMED`; penalty 2 HRC, reward/stake return 0;
available 18 → 18 HRC, locked 0. End duplicate true; pool/escrow invariant true.
Tx `0xb25a50e57c6f2c133afe6cd1a8d0ec2fb9ecccb7e056eeb8843d3aa8898eccda`.
API đã khởi động lại với đúng env/deployment cũ; login token in-memory cần ký
đăng nhập lại. Browser verifier PASS 9 scenario, timer layout desktop/mobile có
fixture UI (không broadcast), còn runtime End dùng dữ liệu IoT/chain thật.

## R22 — Reward session integrity and production hardening — 2026-10-12

Người dùng duyệt triển khai các vấn đề P0–P2. Phạm vi được chia thành các
milestone có kiểm thử riêng; ưu tiên xử lý trực tiếp P0 trước. Checkpoint/task
tool OpenCode không khả dụng trong phiên Codex này; không tạo session/ID giả.

| Milestone | Phạm vi | Tiêu chí nghiệm thu | Trạng thái |
| --- | --- | --- | --- |
| R22a P0 contract | `HRCSessionRewards.sol`, `HRCReward.js`, `DemoLiveSessions.js` | Không đổi policy khi còn escrow; settlement/preview không thưởng dưới min duration; token balance = pool + escrow sau settlement | source/tests done; redeploy pending |
| R22b P0 telemetry policy | reward policy, ingestion stats, live session API và tests | 10 Hz/30s cần xấp xỉ 300 raw mẫu; tolerance, cadence/max-gap/timing-span và processed/raw coverage; API metadata khớp evaluator | done (source/tests) |
| R22c P1 start flow | `confirm-start`, wallet preparation, SQLite session store, IoT lease/intent | Receipt/event/chain/to/from được xác minh; intent + device TTL lease và policy snapshot trước khi trả tx; chỉ mở sensor sau xác minh | done (source/tests); production persistence/concurrency verification pending |
| R22d P1 identity/session | customer/demo auth, SQLite, dashboard | Một identity theo wallet; không phát reward token riêng; app session hash/TTL/revoke; logout đồng bộ | partial; full completion pending production migration/session lifecycle review |
| R22e P1 chain support | wallet/reward services và config/tests | Demo local tách production; Sepolia/Amoy theo deployment config, không hard-code chain 31337 trên production path | partial; real deployment/RPC verification pending |
| R22f P2 admin/security | admin auth/routes, governance, dashboard/nginx | SIWE/RBAC thay token tĩnh/actor header; policy multisig/timelock; CSP/HSTS/frame protection/rate limit/HTTPS guidance | partial; TLS terminator, multisig/timelock deployment and browser CSP verification pending |

R22a cần redeploy và cập nhật deployment artifact khi xác minh trên chain;
không reset chain hoặc ghi đè deployment đang dùng trước khi kiểm tra runtime.
R22c–R22f cần review API compatibility và migration trước khi thay đổi
identity/session hiện hữu.

R22a/R22b/R22c evidence: `npm --prefix smart_contracts test` **83 passing**;
`.venv/bin/python -m pytest tests/ -q` **449 passed, 12 skipped**;
`npm --prefix blockchain_api test` **150 passed**; focused live-session tests
**21 passed**; `git diff --check` PASS.
Contract dùng guard `escrowedStakeTotal > 0`, không snapshot policy. Reward dưới
30 giây là 0; stake vẫn được trả với outcome hợp lệ. Reward policy yêu cầu tối
thiểu 90% mẫu kỳ vọng 10 Hz (270 mẫu/30 giây), processed coverage 95%, cadence
8–12 Hz, timing span 90% thời lượng và max gap 500 ms. Public chain và local
deployment hiện hữu chưa được redeploy.

R22c evidence: wallet receipt checks now validate configured chain, sender,
contract destination, successful receipt, transaction hash, `startSession`
calldata/session key, and matching `SessionStarted` beneficiary/amount. Start
preparation reserves a 120-second device intent and records a policy snapshot
before returning calldata. Tests cover pending receipts, malformed/mismatched
receipt fields, lease contention and expiry. Focused live-session tests: **21
passed**.

R22d–f code is partial and must not be described as production-complete. SIWE
identity/session changes, chain-config routing, admin RBAC and local security
headers have source/tests, but real SQLite migration on deployment, Sepolia/Amoy
RPC plus deployed contracts, browser CSP under the actual proxy, HTTPS/TLS
termination, and Safe/timelock ownership transfer have not been verified. No
chain deployment or governance transfer was performed. Keep these gates open.

## R23 — Freeze reward duration at sensor close — 2026-10-12

| Task | Scope | Acceptance | Status |
| --- | --- | --- | --- |
| R23a:contract | `HRCSessionRewards.sol`, `HRCReward.js`, `DemoLiveSessions.js` | Settlement and preview receive `endedAt`; require `startedAt <= endedAt <= block.timestamp`; reward uses `endedAt - startedAt`; persist and emit the boundary | source/tests done; redeploy pending |
| R23b:backend | `rewardService.js`, `liveSessionService.js`, read/demo wallet ABI, IoT reward client/outbox | Convert frozen ISO-8601 `ended_at` to Unix seconds; bind hash to the same immutable evidence; retries reuse `endedAt` | source/tests done |
| R23c:docs | integration guide, plan and project memory | Explain recovery boundary and ABI/deployment gate; do not mutate the active deployment manifest | done |

R23 regression evidence: delayed settlement at chain +60 with a sensor end at
30 seconds pays 3 HRC; a frozen 45-second end pays 4.5 HRC despite delayed
settlement. Earlier-than-start and future end timestamps revert. Preview and
settlement match; retry preserves the original `endedAt` and cannot settle
twice. Existing deployments retain their old ABI and settlement-time duration
until a new deployment and manifest switch are explicitly verified.

## R24 — MetaMask + SIWE only — 2026-10-12

| Task | Scope | Acceptance | Status |
| --- | --- | --- | --- |
| R24a:auth/UI | blockchain API auth/routes, dashboard and tests | Remove demo-account login and fixed-account UI; wallet address is the single application/reward identity; SIWE is the only app login | source/tests present |
| R24b:local runtime | launcher, local chain funding, operator config | Local launcher uses ports 8081/3000/8545/8002/1886, exact SIWE origin `http://localhost:8081`, local-only funded wallet/key file mode 0600, and refuses occupied ports before deploy | source present; do not run while an existing stack is active |
| R24c:compat/docs | session persistence, README and integration docs | Store module naming is wallet-neutral while legacy JSON paths and SQLite schema remain readable; active docs contain no demo launcher/verifier path | done |

Verification: blockchain API **147/147**, Hardhat **88/88**, dashboard/API JS
syntax, local launcher shell syntax, active-doc stale-reference scan and
`git diff --check` pass. Read-only GETs confirm the already-running dashboard/API
use chain `31337` and SIWE origin `http://localhost:8081`. No live-stack restart,
chain deployment, data reset, or write-producing smoke request was performed.
Preserve the user-owned ESP32 VS Code port change and exclude it from staging.

## R25 — HRC Safe-to-Earn V3 implementation (2026-10-12)

Approved scope: implement the software/local roadmap in
`docs/complete-product-implementation-plan.md`; preserve the running local
deployment and all user-owned workspace changes. No hardware flash, public
testnet deployment, or active-chain migration. Keep HRCSessionRewards V1
semantics intact; V3 uses a separate contract and versioned deployment.

| Task key | Goal / allowed files | Acceptance / checks | Status |
| --- | --- | --- | --- |
| R25:orchestrator | `PLAN.md`, `PROJECT_MEMORY.md`; inspect complete V3 dependencies | Existing policy, identity, sensor and chain behavior audited; no active deployment/data reset | done |
| R25a:orchestrator | Blockchain API SQLite stores/services/routes/tests; local launcher | SIWE local funding is allowlisted to chain 31337, one-time and idempotent, capped, best-effort, visible; task/assignment/policy snapshot/completion/audit APIs enforce RBAC and idempotency | done (API/local tests) |
| R25b:orchestrator | New `HRCTaskRewards.sol`, deploy script, tests, versioned manifest/ABI selection | Approved 0.200/0.002/0.010 HRC economics, frozen per-session amounts/hash, secure settlement/refund, exact accounting; V1 unchanged | done (98 Hardhat tests; isolated deployment smoke) |
| R25c:orchestrator | IoT reward policy/API bridge, blockchain outbox/reconciliation and tests | Completion and telemetry evidence freeze once; deterministic outcomes; immutable retry payload, receipt/event reconciliation, no duplicate settlement | partial (V2 real-contract lifecycle integration drives real Python IoT HTTP/SQLite for safe, single-spike warning, confirmed violation, sensor fault, insufficient coverage and delayed-RPC recovery; sustained-vibration/boundary matrix and chaos injection open) |
| R25d:orchestrator | Dashboard task/session/owner/audit surfaces and browser/API tests | Wallet can discover/accept task, see funding state and session/settlement states; privileged actions server-authorized and audited | partial (UI/API implemented; V2 timer now uses session policy snapshot for 120s/1,080 samples; browser visual/interaction verification open) |
| R25e:orchestrator | Firmware/edge model provenance/API and isolated local E2E/docs | Existing V1 stack remains untouched; new isolated V3 stack exercises safe, warning, spike, violation, fault, chain outage and unauthorized actor; external HIL/public-chain gates remain explicit | partial (safe/spike warning/incomplete/violation/fault against the real V2 contract and real Python IoT HTTP service; chain-outage and unauthorized-actor cases, edge parity, ESP32 HIL and public testnet open) |

Implementation evidence: `npm --prefix blockchain_api test` — 180 passed;
`.venv/bin/python -m pytest tests/ -q` — 449 passed, 12 skipped;
`npx hardhat test` from `smart_contracts/` — 105 passing, including a new V2
task lifecycle integration suite that runs the production wallet calldata
builder, owner completion service, live-session coordinator, durable transaction
writer, and settlement service against the actual deployed `HRCTaskRewards`
contract. Safe retry checks that the on-chain end timestamp/evidence hash remain frozen and accounting
balances; incomplete, confirmed-violation, and sensor-fault outcomes are also
covered. The suite also boots the real Python FastAPI ingestion/session service
on an ephemeral port with its own SQLite database and drives it over HTTP through
`IotSessionClient`; only MQTT transport, device ACKs and wall-clock are
deterministic. Safe, single-spike, confirmed-violation, sensor-fault and
insufficient-coverage telemetry all settle once with correct outcomes, penalties
and `accountingInvariant()`; recovery after coordinator recreation keeps the
frozen end timestamp/evidence hash and does not re-close the sensor. No ESP32,
broker or public chain is involved, and no production endpoint or data is used.
`blockchain_api/taskEvidencePolicy.js` is now the single source for the frozen
evidence policy: task snapshots carry it, and `LiveSessionService` re-derives
duration, raw/processed coverage, cadence, timing span/gap and gap/violation
counts from the raw session boundary instead of trusting the IoT `result` flags.
An `INCOMPLETE` verdict also clears `dataValid`/`coverageValid`, and V2 dashboard
outcome labels read actual payout so a refunded stake is never shown as
`REWARDED`.
V2 wallet responses now include the persisted task/assignment policy snapshot;
API readiness and dashboard guidance consistently show the snapshotted
120-second minimum and 1,080 samples at 10 Hz (90% coverage), while V1 remains
30 seconds / 270 samples. Regression coverage verifies these contracts.
Syntax checks and `git diff --check` pass. Isolated V2 launcher smoke on RPC/API
ports 18545/13000
deployed the versioned contract, verified SIWE customer/admin roles, wallet pool
readback, task create/publish/accept and a safety transaction. It did not start
Docker/IoT, use a sensor, or settle a task. The live ports 8081/3000/8545/8002/1886
were not touched. Preserve `firmware/esp32/.vscode/settings.json` (`/dev/ttyACM0`)
and exclude it from changes. Remaining gates are real IoT backend-to-chain V2
E2E and recovery/chaos matrix, dashboard browser/visual validation, model
provenance and edge parity, hardware/HIL, public testnet, TLS and multisig/timelock
deployment.
Checkpoint plugin tools are not available in this Codex session; no external
checkpoint IDs are claimed.

## R26 — V2 boundary, recovery and authority hardening (2026-10-12)

| Task key | Goal / changed surface | Evidence | Status |
| --- | --- | --- | --- |
| R26:policy | Make task outcome selection fail closed for safe, confirmed violation and sensor-fault claims; reject forged result strings and enforce exact telemetry boundaries | `blockchain_api/taskEvidencePolicy.js`, `liveSessionService.js`; boundary matrix covers 120s/119.999s, 90%/one-under samples, 95% processed coverage, 8/12 Hz, 0.5s/over-gap, sequence gap, reboot and sustained/spike patterns | done |
| R26:writer | Make durable signed transaction recovery chain-configurable and exercise SQLite reopen, pending receipt, lost broadcast response and no duplicate settlement | `durableContractWriter.js`, `walletService.js`, V2 lifecycle integration; exact raw bytes/nonce, one `SessionSettled`, immutable endedAt/evidenceHash | done |
| R26:authority | Remove user-controlled V2 settlement payloads from the legacy endpoint; require SIWE operator/admin and settle only an empty request against frozen evidence | `server.js`, `settleFrozen()`, SIWE HTTP integration; unauthorized/wrong-owner/beneficiary/outcome/amount attempts show no token movement | done |
| R26:browser | Verify the running dashboard proxy and MetaMask-compatible browser flow without restarting active ports | `scripts/verify_wallet_first_ui.mjs` attempted; blocked because `http://localhost:8081/api/chain/api/config` returned HTTP 503 and no Blockchain API/RPC listener was present | blocked by runtime availability |

Latest isolated verification after R26: Blockchain API **197/197**, Hardhat
**118/118**, Python IoT/model **449 passed, 12 skipped**; `git diff --check`
and JavaScript/Python syntax checks pass. The active contract/deployment and
user-owned firmware port setting were not changed. Public testnet, hardware/HIL,
edge parity, TLS termination and multisig/timelock transfer remain external gates.

## R27 — Visible wallet actions and approved local task setup (2026-10-12)

Approved by user `ok` after the missing Start/disabled Approve diagnosis.

| Task key | Goal / allowed files | Acceptance / checks | Status |
| --- | --- | --- | --- |
| R27a:orchestrator | `dashboard/app.js`, `dashboard/index.html`, `blockchain_api/tests/walletDashboard.test.js`, `scripts/verify_wallet_first_ui.mjs` | Customer Start remains visible with an accessible blocking reason; enough allowance is explicitly marked approved; policy-driven guidance has no fixed 2 HRC; task load errors fail closed; blocked clicks create no pending reference or transaction | done: 25 dashboard tests; 15 browser scenarios |
| R27b:orchestrator | Existing local runtime SQLite through SIWE admin HTTP APIs only | Create/publish one identifiable local task, without accepting on behalf of the customer, changing RBAC, restarting/deploying/resetting the chain or faking completion | done: one OPEN task published through SIWE admin; customer assignment left to the user |
| R27c:orchestrator | `PLAN.md`, `PROJECT_MEMORY.md`; tests/browser output in ignored results | API tests, real browser verification, syntax and diff checks; read back existing customer allowance/balance and open task | done: API 203/203; browser 15/15; syntax/diff checks pass |

Checkpoint tools are unavailable here; inspect the existing checkpoint read-only,
work locally without worker dispatch, and preserve the user-owned firmware
VS Code `/dev/ttyACM0` change. Never include runtime keys/tokens in source or output.

R27 evidence: existing runtime task `task-7c2975f5-4f23-4cd4-a550-3ea738346f6d`
(`HRC Local — ESP32 safety task`) is OPEN on device `esp32_01`, one required
cycle, 120s minimum/900s maximum, 0.200 HRC stake, 0.002 reward and 0.010 penalty.
Creation/publication used the existing local admin key to sign SIWE and the
RBAC/audited HTTP routes; no key/token was added to source/output. No customer
assignment or completion evidence was created. Browser checks use an injected
wallet provider with real SIWE/API, deterministic rendering fixtures and no
approval/stake broadcasts; they do not prove real extension signing or hardware
session execution. Screenshots/verification.json are in ignored
`data/results/wallet-first-connect/`. Real latest sensor response still has
`latest = null`; Start remains visibly blocked until recent ESP32 telemetry is
available after the user accepts/selects the task. Existing customer balance
20 HRC, allowance 0.2 HRC and nonce 1 were verified unchanged. Dashboard files
are served from the existing bind mount, with the app.js cache version updated.
No live service restart or contract migration was performed.

## R28 — Task reward duration curve (2026-10-12)

Approved behavior: task sessions are globally eligible from 30 seconds. A safe
session earns 0.001 HRC at 30 seconds, increases linearly to 0.002 HRC at the
task's frozen `maximumDurationS`, and remains eligible at the capped 0.002 HRC
for any longer duration. `maximumDurationS` is therefore the full-reward
threshold, not an upper eligibility bound. Unsafe, invalid or incomplete
evidence remains ineligible for a safe reward.

| Task key | Goal / allowed files | Acceptance / checks | Status |
| --- | --- | --- | --- |
| R28:coder | `HRCTaskRewards.sol`, task deployment/ABI services, evidence/task/live-session services, dashboard, directly affected tests | Contract freezes the full-reward duration at Start; 29s rejects, 30s pays 0.001, midpoint is linear, threshold/over-threshold pay 0.002; backend accepts over-threshold evidence and uses a 30s floor; UI defaults to 30/180 and explains the threshold | done |
| R28:reviewer | Read-only review of R28 diff | Check ABI tuple/event indexes, accounting/daily cap, retry immutability, evidence fail-closed behavior and V1 compatibility | PASS WITH NOTES; base-reward preflight fixed; remaining low-risk receipt-matrix gap covered by integration happy path |
| R28:tester | Read-only test/build verification | Blockchain API and Hardhat suites, JS syntax and `git diff --check` pass | done: API 207/207; Hardhat 122/122 |

This is an ABI-changing V2 task-contract update. The existing deployment and
active Docker/local runtime retain the old ABI and 120-second policy until a
fresh explicit deployment is launched. Do not restart, reset, migrate or
overwrite the running deployment as part of R28. Source verification is
complete; deployment/redeployment remains an explicit follow-up gate.

## R29 — Source-control benchmark evidence (2026-10-13)

User decision: benchmark/evaluation output under `data/results/` is reviewed
evidence and must be committed, unlike runtime state. `data/*` stays ignored;
`data/results/` is re-included with an explicit `.gitignore` exception, and
`data/results/**/*.pid` stays ignored because it is a runtime process artifact.

| Task key | Goal / allowed files | Acceptance / checks | Status |
| --- | --- | --- | --- |
| R29:orchestrator | `.gitignore`, `PLAN.md`, `PROJECT_MEMORY.md`, `data/results/**` | Secret scan of all text evidence finds no key/token/mnemonic; only runtime `.pid` excluded; benchmark reports, metrics, logs and screenshots staged | done |

Restart/redeploy of the local stack is **not** part of R29 and is still gated on
explicit user approval of the destructive option (fresh chain vs. new rewards
contract on the existing chain).

## R30 — Reset local chain onto rewards V2 (2026-10-13)

User decision: option A, reset the local demo chain. The old `hrcdemo-*`
Docker stacks (from the retired `run_local_demo.sh` launcher) were stopped and
their volumes removed, freeing the dashboard/API/MQTT ports. The current
launcher was then started with `--task-rewards-v2`, producing a fresh run
directory and a fresh V2 task-rewards deployment.

| Task key | Goal / allowed files | Acceptance / checks | Status |
| --- | --- | --- | --- |
| R30:orchestrator | Local runtime only; `scripts/run_local_metamask.sh`, `PLAN.md`, `PROJECT_MEMORY.md` | V2 contract live, on-chain curve verified, task 30/180 created+published+accepted, 4-arg Start calldata frozen with the task's `maximumDurationS`; launcher smoke no longer hardcodes the stale 120/120 rule | done |

Verification on the reset chain:

- Rewards revision `task-rewards-v2-duration-curve-v1`, pool 1000 HRC.
- `MIN_SAFE_DURATION = 30`; `rewardAmountForDuration(29,180)` returns 0 (safe settlement below 30s reverts),
  `30s -> 0.001`, `105s -> 0.0015`, `180s/181s/3600s -> 0.002`.
- Task create with `minimumDurationS: 30, maximumDurationS: 180` succeeds; this
  was the operation that failed with "duration must be between 120 seconds and
  24 hours" against the stale deployment.
- Wallet Start calldata decodes as
  `startSession(bytes32 sessionId, bytes32 taskIdHash, bytes32 policyHash,
  uint256 fullRewardDuration)` and the frozen `fullRewardDuration` equals the
  task's `maximumDurationS`.

Note: the retired launcher `run_local_demo.sh` and its `.runtime/local-demo/`
run directories are historical; the supported entrypoint is
`scripts/run_local_metamask.sh`.

## R31 — Task completion guidance and HRC reports (2026-10-13)

Continue the authorized reward/dashboard fixes directly. Read-only inspection
of the reported session confirmed 197.568s, 1,974 clear samples and all frozen
telemetry checks passed; completion evidence was absent. Reports currently
loads only safety logs and omits HRC sessions. No runtime evidence or chain
records are to be modified to manufacture a reward.

| Task key | Goal / allowed files | Acceptance / checks | Status |
| --- | --- | --- | --- |
| R31:orchestrator | `dashboard/app.js`, `dashboard/index.html`, `blockchain_api/liveSessionService.js`, `blockchain_api/server.js`, focused API/dashboard tests, `PLAN.md`, `PROJECT_MEMORY.md` | Show current/frozen completion review state before End; preserve customer isolation and operator authority; Reports includes refunded/paid HRC sessions and customer transactions; stake subtitle uses receipt amounts; test API, race/error states, browser rendering | done |

Implementation and verification:

- `GET /api/admin/wallet/sessions` now exposes settled and active HRC sessions to
  admin/operator wallets without creating approvals or settlements. Customer
  reports remain limited to that wallet's `/api/me/sessions` and transactions.
- Customer wallet guidance now reports missing, insufficient, approved and frozen
  task-cycle evidence before End. End remains available so the sensor can stop;
  missing completion evidence settles safely as an HRC refund with no reward.
- The stake ledger derives its amount from the SessionStarted event instead of
  displaying the stale 2 HRC V1 label. Settled assignments are no longer shown
  as selectable task assignments.
- API tests: **214/214**. Focused live/task/dashboard tests: **55/55**.
  Hardhat tests: **122/122**. Existing wallet-first browser verifier: **15/15**;
  custom report/completion Chrome checks passed on desktop and mobile with zero
  broadcasts and no horizontal overflow.
- The active local API was reloaded in place. Chain genesis, V2 deployment,
  settlement transaction `0x4ad6a7db4a0bf008a22c6b19eb39c837fa370d6a950933e1342f2e027e9b796c`,
  and all five session records were verified unchanged. The reported session
  remains final: `INCOMPLETE`, stake returned `0.2 HRC`, reward `0 HRC`, reason
  `task_completion_evidence_missing`.

Checkpoint tool is unavailable in this Codex session; historical OpenCode state
was read but remains unchanged.


## R32 — Visible, durable task-cycle progress (2026-10-13)

User authorized implementation after reporting that changing the target still
leaves the completed cycle count unknown. The existing HC-SR04 telemetry has
no robot action/completion signal; distance or elapsed time must not fabricate
robot cycles. Use audited operator-recorded cycle events, show current/required
and remaining counts, and freeze owner-approved completion automatically when
the recorded target is reached. Keep existing manual approval compatibility.

| Task key | Goal / allowed files | Acceptance / checks | Status |
| --- | --- | --- | --- |
| R32:orchestrator-source | `blockchain_api/taskService.js`, `sqliteStores.js`, `liveSessionService.js`, `server.js`, directly affected tests; `dashboard/app.js`, `index.html`, `style.css`; isolated lifecycle integration | Durable idempotent operator events; customer cannot increment; only RUNNING/unfrozen sessions; count and remaining visible; target auto-approval from audited events; retries/reopen/final evidence immutable; existing V1 works | done |
| R32:orchestrator | `PLAN.md`, `PROJECT_MEMORY.md`, dedicated cycle browser verifier/evidence, local API reload | Review implementation and focused/full tests; browser desktop/mobile; reload API preserving chain and existing settlements | done |

Checkpoint/task plugin and GitNexus MCP are unavailable in this Codex session;
source inspection replaces graph tracing, and historical checkpoint state is
read-only. No chain reset, contract deployment, firmware flash or manufactured
runtime completion is authorized by this change.

R32 verification: Blockchain API **224/224**; isolated real-contract lifecycle
**26/26**, including 1/2 recorded actions -> refund/no reward and 2/2 ->
0.2 HRC stake refund + 0.002 HRC reward, SQLite reopen and immutable delayed
settlement retry. Chrome **21/21** desktop/mobile scenarios, no JavaScript
exceptions or wallet broadcasts. Browser fixtures simulate cycle writes locally
and do not manufacture completion evidence on the live API. Syntax and
`git diff --check` pass. Evidence: `data/results/task-cycle-progress-r32/`.

The local API was reloaded with its existing environment. Chain genesis and
reward deployment manifest are unchanged; all **9** existing session records
retain their exact digest. The new SQLite table contains **0** live cycle events
until an operator records a real observed action. Hardware does not count robot
cycles automatically: the delivered count is explicitly operator-recorded.
Manual final approval cannot override a nonzero event counter with another
value; ongoing work uses cycle events until the target is reached.
Implementation/review/testing were performed directly; no worker dispatched.


## R33 — Automatic user activity time (2026-10-13)

User clarified that the intended goal is to detect user movement and prevent standing still from satisfying completion, then approved measuring active time. R32 operator-recorded robot cycles did not meet this intent. New tasks default to a sensor_activity rule requiring 20 active seconds plus the existing 30-second session minimum; published tasks and accepted policy snapshots retain their original rules.

| Task key | Scope and acceptance | Status |
| --- | --- | --- |
| R33:coder-activity | Python deterministic median-filtered activity statistics from safe session-bound raw telemetry; no credit for noise/spikes/gaps/faults; stable evidence hash and reopen/isolation tests | done |
| R33:orchestrator | Frozen activity rule, automatic completion/evidence validation, no manual override; UI active/stationary seconds; API/UI/real-contract checks, docs, launcher smoke activity rule and local reload preserving chain | done |

Released demo policy: trailing median of 5 samples, 1-second movement window with distance range at least 10mm; only consecutive time-synced valid CLEAR evidence and intervals <=0.5s receive activity credit. No wall-clock waiting credit. Sensor activity reflects distance variation rather than identity, steps or a walking route. No live sensor samples or activity evidence will be manufactured.

R33 verification: Python **118/118** (55 activity cases); Blockchain API **233/233**; isolated real-contract lifecycle **32/32**; Chrome **25/25** desktop/mobile scenarios, zero JS exceptions and zero wallet broadcasts. New integration cases show stationary/noise/spike/short movement -> 0.2 HRC stake refund and no reward; sufficient movement at 120s and 180s -> stake refund plus capped 0.002 HRC without manual completion. SQLite reopen preserves evidence and one settlement event. Bash/JS/Python syntax and diff whitespace checks pass. Launcher activity smoke is syntax-checked; no fresh-chain launcher was run on the active runtime.

Local API PID **616502** and rebuilt IoT container are serving R33. Before reload no sensor session was running. Chain genesis, manifest, all **10 wallet-session records** and **8 existing task definitions** retain their exact digests. Through existing SIWE admin routes, published one OPEN task **Active movement · 20s automatic**, task ID `task-8f158b8a-f1b1-43d9-8e8d-add83fccd546`, requiredActiveSeconds 20, minimumDurationS 30, full reward at 50s. No customer assignment, live completion/activity evidence, robot action or retroactive reward was created. Existing sensors are read-only for verification; hardware movement detection has not been physically exercised. Evidence: `data/results/user-activity-r33/`. Changes remain uncommitted.
