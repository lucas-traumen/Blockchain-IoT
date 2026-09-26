# Quy trình thu dữ liệu HRC Safety Log

Ba cách thu dữ liệu: (1) thiết bị thật qua MQTT, (2) dataset synthetic không cần
phần cứng, (3) device simulator (chưa có — dự kiến M7c). Sau đó nạp/replay/export
bằng các CLI trong `scripts/`. Mọi lệnh chạy từ repo root bằng venv `.venv/`.

## 1. Quy ước session

- Ranh giới session chốt theo **ack thiết bị** cho lệnh `session_start`/
  `session_stop` (`boundary_source = device_ack`): mẫu đầu session là
  `ack.sequence + 1` tại lệnh start; lệnh stop nằm trong session
  (`[start_sequence, stop_sequence]`, `stop_sequence = ack.sequence` của lệnh
  stop). Không gán theo giờ nhận MQTT.
- **Fallback** khi không nhận được ack trong `HRC_ACK_TIMEOUT_S` (mặc định 2 s)
  hoặc chạy CLI `--no-mqtt`: ranh giới start = sequence lớn nhất đã nhận của
  boot hiện hành tại thời điểm start (`boundary_source = db_fallback`).
- Boot restart giữa session (`boot_id` đổi): mẫu của boot mới **không gán** vào
  session, session bị đánh dấu `boot_change = 1`.
- Bảng `sessions` lưu `scenario`, `object`, `zone_note`, `start_boot_id`,
  `start_sequence`, `stop_sequence` để truy vết.
- Cảnh báo vận hành: không chạy `session_cli --mqtt` song song với backend trên
  cùng một DB — một thời điểm chỉ một tiến trình ghi DB.

## 2. Lệnh session CLI (`scripts/session_cli.py`)

Mặc định `--no-mqtt` (không cần broker, ranh giới theo db_fallback); `--mqtt`
publish control và chờ ack thiết bị. `--mqtt`/`--no-mqtt` loại trừ nhau.

```bash
# Bắt đầu session (mọi option tùy chọn, trừ các cờ mqtt)
.venv/bin/python -m scripts.session_cli start --scenario demo
.venv/bin/python -m scripts.session_cli start \
    --scenario approach --object nguoi --zone-note "ban cong A" \
    --session-id run_001 --mqtt

# Đóng session đang chạy (--session-id bỏ qua = session đang mở)
.venv/bin/python -m scripts.session_cli stop --session-id run_001

# Liệt kê mọi session / xem chi tiết một session
.venv/bin/python -m scripts.session_cli list
.venv/bin/python -m scripts.session_cli status --session-id run_001

# Export raw samples của session
.venv/bin/python -m scripts.session_cli export --session-id run_001 \
    --format jsonl --out data/run_001.jsonl
.venv/bin/python -m scripts.session_cli export --session-id run_001 \
    --format csv --out data/run_001.csv
```

Option chung (đặt trước subcommand): `--db <đường dẫn>`, `--safety-config <yaml>`,
`--device-id <id>`, `--env-file <file>`.

Tương đương qua API backend đang chạy:

```bash
curl -X POST http://localhost:8000/sessions/start \
     -H 'Content-Type: application/json' -d '{"scenario": "demo", "session_id": "run_001"}'
curl -X POST http://localhost:8000/sessions/run_001/stop
```

## 3. Thu với thiết bị thật

1. Khởi động hệ thống theo [wiring.md](wiring.md): broker → backend → firmware.
2. Mở session: `session_cli start ... --mqtt` (hoặc POST `/sessions/start`).
3. Thực hiện kịch bản di chuyển vật/người quanh robot trong thời gian thu.
4. Đóng session: `session_cli stop --session-id <id>` (hoặc POST stop).
5. Export JSONL/CSV: lệnh ở mục 2, hoặc
   `curl -o data/run_001.jsonl 'http://localhost:8000/sessions/run_001/export?format=jsonl'`.

Telemetry 10 Hz tự ingest khi backend đang chạy (`HRC_MQTT_ENABLED=true`); safety
+ dự báo được tính ngay (`processor_version = safety-v1`, `mode = live`) và lưu
vào `processed_samples`.

## 4. Thu không cần phần cứng

### 4a. Dataset synthetic (`scripts/synthetic_dataset.py`)

Sinh JSONL telemetry_v1, **dữ liệu tổng hợp có nhãn bắt buộc** trên từng dòng:
`"synthetic": true`, `"scenario"`, `"session_label"` (quy ước dự án: không được
trình bày như dữ liệu đo thật). Kịch bản `--scenario`:

| Giá trị | Mô tả |
|---------|-------|
| `all` (mặc định) | Xen kẽ 4 kịch bản theo thứ tự session |
| `approach` | Tiến gần từ ~1 m, có crossing xuống dưới Sp demo 440 mm |
| `recede` | Lùi từ ~0.2 m ra ~1 m |
| `mixed` | Xen kẽ 4–6 pha approach/recede |
| `noise` | Dao động quanh 0.5–0.9 m, không chạm ngưỡng |

```bash
.venv/bin/python -m scripts.synthetic_dataset --out data/sim.jsonl \
    --sessions 30 --samples-per-session 200 --seed 42 --scenario all
```

Cùng `--seed` cho ra dataset giống hệt nhau. Output in summary JSON
(`n_sessions`, `n_samples`, `n_error_samples`, `scenario_counts`, `seed`).

**Lưu ý khi nạp file synthetic vào DB:** schema telemetry_v1 cài
`additionalProperties: false` nên phải tách 3 nhãn metadata trước khi ingest
(việc tách này cũng là điều `ai_model/prepare.py:load_jsonl_dataset` làm khi train):

```bash
.venv/bin/python - <<'PY'
import json
META = ("synthetic", "scenario", "session_label")
with open("data/sim.jsonl", encoding="utf-8") as fin, \
     open("data/sim_ingest.jsonl", "w", encoding="utf-8") as fout:
    for raw in fin:
        row = json.loads(raw)
        fout.write(json.dumps({k: v for k, v in row.items() if k not in META}) + "\n")
PY
```

### 4b. Device simulator

`scripts/device_sim.py` **chưa có trong repo** (dự kiến bổ sung ở M7c) — hiện
chỉ có synthetic dataset là cách mô phỏng không cần phần cứng.

## 5. Nạp JSONL vào DB (`scripts/ingest_jsonl.py`)

```bash
.venv/bin/python -m scripts.ingest_jsonl --input data/sim_ingest.jsonl
```

Option: `--input <file>` (bắt buộc), `--db <đường dẫn>`, `--safety-config <yaml>`,
`--model-dir <thư mục artifact>`, `--env-file <file>`.

Kết quả in summary JSON với 4 bộ đếm:

| Bộ đếm | Ý nghĩa |
|--------|---------|
| `stored` | Mẫu hợp lệ, nạp mới vào `raw_samples` |
| `duplicate` | Trùng khóa `(device_id, boot_id, sequence)` — bỏ qua |
| `rejected` | Đúng JSON nhưng vi phạm schema/enum — ghi vào `rejected_messages` kèm payload thô |
| `invalid_lines` | Dòng không parse được JSON / không phải object / thiếu `device_id` |

CLI luôn exit 0 kể cả khi có rejected (dữ liệu xấu được ghi nhận, không phải lỗi
chương trình); cuối cùng chạy `catch_up()` tính nốt raw pending. Nếu `--model-dir`
( mặc định từ `MODEL_ARTIFACT_DIR`) có artifact, mỗi mẫu nạp kèm dự báo —
thiếu artifact thì prediction `MODEL_UNAVAILABLE`, safety vẫn tính.

## 6. Replay offline (`scripts/replay.py`) và export cho TV2

Replay tính lại safety cho raw đã lưu, **cùng code path với live** nhưng ghi với
`processor_version` riêng (mặc định `replay-safety-v1`) và `mode = offline`
(`stale` luôn false — không so tuổi mẫu với giờ chạy). Idempotent: chạy lại cùng
processor_version → 0 mẫu mới.

```bash
# Replay toàn bộ raw trong DB
.venv/bin/python -m scripts.replay --db data/hrc.db

# Replay một session, kèm dự báo từ artifact
.venv/bin/python -m scripts.replay --db data/hrc.db --session-id run_001 \
    --processor-version replay-safety-v1 --model-dir ai_model/artifacts
```

Option: `--db`, `--safety-config`, `--session-id` (bỏ qua = mọi raw),
`--processor-version` (mặc định `replay-safety-v1`), `--model-dir`, `--env-file`.
Summary in ra: `{"processed": N, "by_status": {"CLEAR": .., "VIOLATION": .., "UNKNOWN": ..}, "mode": "offline"}`.

Ý nghĩa `processor_version`/`mode` trong `processed_samples` (bản ghi assessment_v1):

- `safety-v1` + `mode = live`: tính lúc ingest từ MQTT/ingest_jsonl; `stale = true`
  nếu mẫu chậm hơn `stale_after_s` (0.5 s theo `configs/model.yaml`).
- `replay-safety-v1` + `mode = offline`: tính lại offline; dùng config safety đúng
  thời điểm mẫu (`config_time_source = measured_at`, fallback `received_at`).
- `prediction.status` ∈ `OK | WARMUP | INVALID_WINDOW | MODEL_UNAVAILABLE`.

Export dữ liệu bàn giao TV2:

```bash
# Raw samples của session dạng JSONL/CSV (CLI)
.venv/bin/python -m scripts.session_cli export --session-id run_001 --format jsonl --out data/run_001.jsonl

# Raw samples qua API (JSONL hoặc CSV)
curl -o data/run_001.jsonl 'http://localhost:8000/sessions/run_001/export?format=jsonl'
curl -o data/run_001.csv 'http://localhost:8000/sessions/run_001/export?format=csv'

# Bản ghi assessment (JSON) — chọn processor_version để lấy bản live hoặc replay
curl 'http://localhost:8000/assessments?session_id=run_001&processor_version=safety-v1'
curl 'http://localhost:8000/assessments?session_id=run_001&processor_version=replay-safety-v1'

# Tra một assessment theo khóa mẫu
curl http://localhost:8000/samples/esp32_01/<boot_id>/125
```

Ghi chú: endpoint `/sessions/{id}/export` trả **raw samples** (11 field telemetry,
JSONL hoặc CSV); bản assessment_v1 (safety + prediction) trả dạng JSON qua
`/assessments` và `/samples/...` — chưa có endpoint CSV riêng cho assessment.
Chi tiết định dạng bàn giao xem [tv2_handoff.md](tv2_handoff.md).
