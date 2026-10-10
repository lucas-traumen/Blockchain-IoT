# Sổ tay thí nghiệm (tái lập được)

Bốn thí nghiệm kiểm chứng pipeline HRC Safety Log từ sinh dữ liệu đến cảnh
báo. Mọi lệnh chạy từ repo root bằng venv `.venv/`, không cần phần cứng
trừ khi ghi rõ. Dữ liệu đầu vào là **dataset synthetic có nhãn**
(`"synthetic": true` — xem [data-collection.md](data-collection.md) mục 4a):
không đại diện cho thực nghiệm phần cứng, chỉ dùng để kiểm chứng cấu trúc
pipeline và hành vi thuật toán.

## Thí nghiệm 1 — Pipeline smoke: sinh → ingest → replay

**Mục tiêu:** chạy trọn pipeline không phần cứng: sinh JSONL → nạp DB (tính
safety live) → replay offline → chứng minh replay idempotent.

**Điều kiện:** venv sẵn sàng; DB ghi vào đường dẫn riêng (`--db`) để không
đụng `data/hrc.db`; không cần broker; không cần model artifact (thiếu
artifact → prediction `MODEL_UNAVAILABLE`, safety vẫn tính).

**Lệnh:**

```bash
# 1. Sinh dataset synthetic 12 session × 80 mẫu, seed 42 (deterministic)
.venv/bin/python -m scripts.synthetic_dataset --out data/sim_smoke.jsonl \
    --sessions 12 --samples-per-session 80 --seed 42

# 2. Tách 3 nhãn metadata (synthetic/scenario/session_label) trước khi ingest
#    — schema telemetry_v1 cài additionalProperties: false
.venv/bin/python - <<'PY'
import json
META = ("synthetic", "scenario", "session_label")
with open("data/sim_smoke.jsonl", encoding="utf-8") as fin, \
     open("data/sim_smoke_ingest.jsonl", "w", encoding="utf-8") as fout:
    for raw in fin:
        row = json.loads(raw)
        fout.write(json.dumps({k: v for k, v in row.items() if k not in META}) + "\n")
PY

# 3. Ingest vào DB riêng (tính safety live, processor_version = safety-v1)
.venv/bin/python -m scripts.ingest_jsonl --input data/sim_smoke_ingest.jsonl \
    --db data/exp1.db

# 4. Replay offline toàn bộ raw (processor_version = replay-safety-v1)
.venv/bin/python -m scripts.replay --db data/exp1.db --processor-version replay-safety-v1

# 5. Replay lần nữa CÙNG version — phải không tạo thêm bản ghi nào
.venv/bin/python -m scripts.replay --db data/exp1.db --processor-version replay-safety-v1
```

**Kết quả kỳ vọng (cấu trúc):**

- Bước 1: summary JSON `n_sessions=12`, `n_samples=960`,
  `scenario_counts` đều 240 mỗi kịch bản; `n_error_samples` khoảng 2% mẫu
  (dao động theo seed — với seed 42 là 14).
- Bước 3: `stored = 960`, `rejected = 0`, `invalid_lines = 0`. Mẫu
  `valid=false` (ECHO_TIMEOUT) **vẫn stored** vì đúng schema — được đánh giá
  UNKNOWN, không bị từ chối. Sau bước này DB có 960 bản ghi `safety-v1`
  (mode `live`).
- Bước 4: `processed = 960` bản ghi `replay-safety-v1` mới (mode `offline`);
  `by_status` chia CLEAR/VIOLATION/UNKNOWN, trong đó **UNKNOWN = số mẫu lỗi**
  (khớp `n_error_samples`), phần còn lại là kết quả ngưỡng trên kịch bản
  approach/mixed (có crossing dưới Sp) và recede/noise (không chạm ngưỡng).
- Bước 5: `processed = 0` — idempotent trong cùng `processor_version`.

**Số liệu quan sát thật** (seed 42, chạy ngày 2026-09-23, DB ghi vào thư mục
tạm — đường dẫn không ảnh hưởng kết quả): ingest `{"stored": 960,
"duplicate": 0, "rejected": 0, "invalid_lines": 0}`; replay lần 1
`{"processed": 960, "by_status": {"CLEAR": 750, "VIOLATION": 196,
"UNKNOWN": 14}, "mode": "offline"}`; replay lần 2 `{"processed": 0, ...}`.
UNKNOWN = 14 đúng bằng `n_error_samples` của dataset.

**Ghi chú giới hạn:** số lượng CLEAR/VIOLATION là đặc tính của dữ liệu tổng
hợp (kịch bản `approach` cố tình xuyên qua ngưỡng Sp demo 0.44 m), không
phải tỷ lệ vi phạm thực tế. Bản ghi trong thí nghiệm này không kèm dự báo
(không `--model-dir`).

## Thí nghiệm 2 — Train + evaluate LSTM

**Mục tiêu:** train LSTM theo session split và đánh giá trên đúng test split
của artifact cùng hai baseline persistence và constant-velocity.

**Điều kiện:** image AI có TensorFlow; dataset có đủ session để chia
70/15/15; không cần iot_backend.

**Lệnh:**

```bash
# 1. Sinh dataset train 30 session × 200 mẫu, seed 42
docker compose --profile ai run --rm ai python -m scripts.synthetic_dataset \
    --out data/sim_train.jsonl --sessions 30 --samples-per-session 200 \
    --seed 42 --scenario all

# 2. Train LSTM; artifact ghi split_manifest.json
docker compose --profile ai run --rm ai python -m ai_model.lstm_train \
    --dataset data/sim_train.jsonl --out-dir ai_model/artifacts/lstm-v1

# 3. Đánh giá LSTM và hai baseline trên test split đã lưu
docker compose --profile ai run --rm ai python -m ai_model.evaluate \
    --dataset data/sim_train.jsonl --model-type lstm \
    --model-dir ai_model/artifacts/lstm-v1 \
    --sp 0.44 --output data/lstm-report.json
```

`evaluate` nhận thêm `--config` (mặc định `configs/model.yaml`), `--sp`
(mặc định 0.04 = 40 mm; synthetic demo bên dưới dùng `--sp 0.44`), `--horizon-index` (mặc định 4 = horizon 500 ms). Evaluator
dùng danh sách session trong `split_manifest.json`, kiểm tra lại session/counts
và chỉ báo cáo test.

**Cách đọc report JSON:**

- `models`: ba model `lstm`, `persistence`, `constant_velocity`; mỗi model có
  regression và event metrics theo 5 horizon.
- `n_test_windows`: số cửa sổ thuộc test split.
- `breakdown`: metrics cô lập theo `scenario` và `target_type`; group thiếu
  metadata đi vào `unknown`.
- `synthetic: true`: nguồn dữ liệu được ghi nhận trong report.
- `sp_m`, `horizon_index`: tham số đã dùng cho cảnh báo và lead time.

**Ghi chú giới hạn:** synthetic có nhãn chuyển động lý tưởng + nhiễu
Gaussian nhỏ; train/evaluate pass không chứng minh chất lượng trên dữ liệu đo
thật. Firmware/HC-SR04 chưa kiểm chứng trên board (xem [wiring.md](wiring.md)).

## Thí nghiệm 3 — Cảnh báo sớm live

**Mục tiêu:** quan sát `prediction.status` chuyển WARMUP → OK và
`early_warning = true` khi chuỗi approach tiến gần chạm Sp.

**Điều kiện:** artifact model đã train (Thí nghiệm 2) hoặc đường dẫn
`MODEL_ARTIFACT_DIR` có đủ năm file artifact LSTM theo [lstm.md](lstm.md). Runtime chỉ dùng LSTM; persistence và constant-velocity chỉ là baseline offline:

- **(a) Backend + device simulator:** khởi động iot_backend theo
  [wiring.md](wiring.md), chạy `python -m scripts.device_sim` với broker đã
  chọn và `--record-jsonl` để giữ nhãn synthetic. Chọn LSTM theo [lstm.md](lstm.md).
- **(b) Ingest JSONL stream không cần broker** (kiểm chứng được ngay):

```bash
.venv-ai/bin/python -m scripts.ingest_jsonl --input data/sim_smoke_ingest.jsonl \
    --db data/exp3.db --model-type lstm --model-dir ai_model/artifacts/lstm-v1

# Quan sát trạng thái prediction theo sequence
.venv/bin/python - <<'PY'
import sqlite3, json
conn = sqlite3.connect("data/exp3.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT result_json FROM processed_samples "
    "WHERE processor_version='safety-v1' AND boot_id="
    "(SELECT MIN(boot_id) FROM processed_samples) ORDER BY sequence"
).fetchall()
for row in rows[:25]:
    r = json.loads(row["result_json"])
    p, s = r["prediction"], r["safety"]
    print(f"seq={r['sample_ref']['sequence']:3d} safety={s['status']:9s} "
          f"pred={p['status']:17s} ew={p['early_warning']} stale={p['stale']}")
PY
```

**Kết quả kỳ vọng:**

- 9 mẫu đầu của mỗi run: `prediction.status = WARMUP` (chưa đủ cửa sổ 10
  mẫu); từ mẫu 10: `OK` kèm `distance_m` (5 dự báo) và `window_ref`.
- Trong pha approach (khoảng cách giảm về dưới Sp demo 0.44 m): các mẫu vẫn
  CLEAR nhưng dự báo chạm ngưỡng → `early_warning = true`; khi khoảng cách
  hiện tại ≤ Sp → `safety.status = VIOLATION` và `early_warning = false`
  (đã vi phạm, không còn là cảnh báo sớm). Kịch bản `noise` không sinh
  `early_warning` (min ≥ ~0.455 m).
- `stale = true` ở mode live khi tuổi mẫu vượt `stale_after_s` (0.5 s) — với
  dataset synthetic, `measured_at` cố định trong quá khứ (mốc 2026-01-01 của
  generator) nên mọi mẫu nạp offline đều `stale = true`: đây là hành vi đúng
  của cờ, không ảnh hưởng early_warning.
- Mẫu lỗi ECHO_TIMEOUT đứt run → `INVALID_WINDOW` tại chính mẫu đó, sau đó
  WARMUP lại cho tới khi run mới đủ 10 mẫu.

**Ghi chú giới hạn:** predictor là instance riêng mỗi tiến trình — buffer
không tồn qua lần chạy khác; quan sát qua iot_backend live dùng device_sim
hoặc thiết bị thật theo [wiring.md](wiring.md).

## Thí nghiệm 4 — Replay khác processor_version trên cùng DB

**Mục tiêu:** chứng minh (i) mỗi `processor_version` là một lần tính độc lập
trên cùng raw, (ii) kết quả safety giống hệt giữa các version, (iii)
idempotent trong cùng version.

**Điều kiện:** DB đã có raw (Thí nghiệm 1); không cần model.

**Lệnh:**

```bash
.venv/bin/python -m scripts.replay --db data/exp1.db --processor-version replay-exp4-a
.venv/bin/python -m scripts.replay --db data/exp1.db --processor-version replay-exp4-b

# Replay lại cùng version — phải 0 mẫu mới (idempotent)
.venv/bin/python -m scripts.replay --db data/exp1.db --processor-version replay-exp4-a

# So sánh hai version trên cùng một mẫu
.venv/bin/python - <<'PY'
import sqlite3, json
conn = sqlite3.connect("data/exp1.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT * FROM processed_samples WHERE processor_version IN "
    "('replay-exp4-a','replay-exp4-b') AND device_id='esp32_sim' "
    "AND sequence=10"
).fetchall()
recs = [json.loads(r["result_json"]) for r in rows]
for r, rec in zip(rows, recs):
    print(r["processor_version"], r["mode"], r["computed_at"],
          rec["safety"]["status"], rec["safety"]["margin_m"])
print("safety giống hệt:", recs[0]["safety"] == recs[1]["safety"])
PY
```

**Kết quả kỳ vọng:**

- Mỗi version tạo đúng một bộ bản ghi riêng (`mode = offline`); DB cùng lúc
  chứa nhiều version cho cùng khóa mẫu — `UNIQUE (device_id, boot_id,
  sequence, processor_version)` phân lập chúng.
- Cùng raw + cùng config → khối `safety` **giống hệt** giữa các version; chỉ
  `computed_at` (và khối prediction nếu model khác) khác nhau.
- Replay lại cùng version → `processed = 0`.

**Số liệu quan sát thật** (trên DB Thí nghiệm 1, seed 42): hai version
`replay-exp4-a/b` mỗi cái `processed = 960` với `by_status` giống hệt
`{CLEAR: 750, VIOLATION: 196, UNKNOWN: 14}`; so sánh mẫu
`esp32_sim/sim_0000002a_0/seq 10` — khối safety đồng nhất
(`margin_m = 0.587`, CLEAR), `computed_at` khác nhau theo từng lần chạy;
DB chứa đồng thời 4 version (`safety-v1` live + 3 version replay).

**Ghi chú giới hạn:** version nào "chuẩn" là quyết định của TV2/orchestrator
dựa trên `processor_version` + `mode` + `computed_at`; iot_backend không tự hợp
nhất các version.

## Giới hạn chung của mọi thí nghiệm

- Dữ liệu synthetic **được ghi nhãn trên từng dòng** (`"synthetic": true`,
  `scenario`, `session_label`) — tuyệt đối không trình bày như dữ liệu đo
  thật; nhãn metadata phải tách ra trước khi ingest vào DB.
- Mọi số liệu trong tài liệu này là kết quả đã chạy thật trên seed/config
  nêu rõ, hoặc kỳ vọng cấu trúc khi chưa chạy — không giá trị nào bịa thêm.
- Firmware đã build PASS trên ESP-IDF 6.0.1 cho ESP32-S3 (2026-10-02),
  nhưng flash/chạy đo HC-SR04 chưa kiểm chứng; các thí nghiệm ở đây không
  phủ nhận phần chưa kiểm chứng của firmware
  ([wiring.md](wiring.md), `firmware/esp32/README.md`).

## Thí nghiệm 5 — LSTM và event package

Theo [lstm.md](lstm.md), train LSTM rồi evaluate với `--model-type lstm`.
Đọc MAE/RMSE theo horizon, F1/miss/FWR/lead-time và breakdown
`scenario`/`target_type`. Kết quả LSTM và hai baseline tham chiếu được giữ nguyên trong report;
không dùng test để chọn epoch hay sửa config.

Chạy simulator qua broker riêng, dùng API mở/đóng session, kiểm tra:
`/health.model_available`, `/assessments` (WARMUP→OK), `/events` và gói raw.
Replay `--model-type lstm --processor-version replay-lstm-v1` hai lần: lần
đầu sinh bộ offline, lần hai `processed=0`. Source recording/dataset phải giữ
nhãn synthetic. Smoke không thay cho dữ liệu board thật, broker reconnect hay
hiệu chuẩn trong phòng thí nghiệm.

### Evidence chạy ngày 02/10/2026

Dataset synthetic 12×80, seed42, test96 windows thuộc sess_2/sess_8; LSTM
3 epochs smoke và report gồm LSTM cùng hai baseline tham chiếu. Đây chỉ là
kiểm chứng cấu trúc pipeline, không phải kết quả dữ liệu thật.

Broker Mosquitto2.0.22 trên cổng riêng11883 + simulator70 mẫu: device_ack
start6/stop55, session50 mẫu; LSTM OK31, VIOLATION11, UNKNOWN2. Replay cùng
DB/model lần đầu70, lần hai0; raw event packages đủ pre/post sau khi bracket
biên do jitter. Đó là smoke software/local broker, chưa kiểm reconnect/soak
trên board. Test cuối: backend310 pass/11 skip, AI321 pass/0 skip.
