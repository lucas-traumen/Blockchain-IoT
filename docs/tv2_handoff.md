# Bàn giao dữ liệu cho TV2 (assessment_v1)

Contract giữa TV3/TV4 (iot_backend thu thập + đánh giá) và TV2 (bộ gom sự kiện /
Smart Contract của TV1–TV2). Đơn vị bàn giao là **bản ghi assessment_v1** lưu
trong cột `processed_samples.result_json` của SQLite — đúng
[schemas/assessment_v1.schema.json](../schemas/assessment_v1.schema.json)
(Draft 2020-12, `additionalProperties: false`). TV2 không đọc code iot_backend;
mọi thứ TV2 cần về một mẫu đo nằm trọn trong bản ghi này.

## 1. Bản ghi assessment_v1

Bản ghi gồm đúng **9 nhóm field** (thiếu/thừa field là vi phạm schema):

| Nhóm field | Kiểu | Nguồn giá trị | Ý nghĩa |
|------------|------|---------------|---------|
| `schema_version` | `"1.0"` | cố định | Phiên bản schema bản ghi |
| `sample_ref` | object | payload telemetry | Khóa mẫu: `device_id`, `boot_id`, `sequence` |
| `session_id` | string/null | `SessionManager.assign` | Lượt thu mẫu thuộc theo ranh giới session; `null` nếu không gán được (ngoài khoảng, boot khác, hoặc ingest không mở session) |
| `measured_at` | string/null | payload telemetry | Giờ thiết bị tự khai lúc **bắt đầu đo**; `null` khi chưa sync GPS/NTP (`time_synced=false`) — không bao giờ thay bằng giờ iot_backend |
| `computed_at` | string | `utc_now_iso()` lúc tính | Thời điểm processor tính; **khác nhau theo `processor_version`** (live tính lúc nhận, replay tính lúc chạy replay) |
| `received_at` | string | giờ iot_backend nhận message | Thời điểm MQTT/ingest nhận payload; catch_up/replay tái dùng giá trị đã lưu trong `raw_samples.received_at`, không đè bằng giờ mới |
| `distance_m` | number/null | `distance_mm / 1000` | Khoảng cách đo (SI tại biên); `null` khi mẫu lỗi (`sensor_valid=false`) |
| `safety` | object | `iot_backend/safety` | Kết quả đánh giá ngưỡng an toàn — 9 field con, xem bảng dưới |
| `prediction` | object | `iot_backend/inference` | Dự báo + cảnh báo sớm — 8 field cũ và 2 field LSTM optional, xem bảng dưới |

### 1.1 Khối `safety` (9 field con)

| Field | Kiểu | Ý nghĩa |
|-------|------|---------|
| `status` | `CLEAR` \| `VIOLATION` \| `UNKNOWN` | `margin_m > 0` → CLEAR; `margin_m <= 0` → VIOLATION (bằng đúng Sp cũng là VIOLATION); mẫu lỗi hoặc thiếu config hiệu lực → UNKNOWN. UNKNOWN không bao giờ được nâng thành CLEAR |
| `sensor_valid` | bool | Đo hợp lệ: `valid=true` và `distance_mm` không null |
| `assessment_valid` | bool | `sensor_valid` **và** có phiên bản config safety hiệu lực tại thời điểm mẫu |
| `sp_m` | number/null | Ngưỡng an toàn `Sp = Sh+Sr+Ss+C+Zd+Zr` (mét); `null` khi UNKNOWN |
| `margin_m` | number/null | `distance_m − sp_m`; `null` khi UNKNOWN |
| `config_version` | string/null | Phiên bản config chọn theo `valid_from <= thời điểm mẫu` (mới nhất trong quá khứ); `null` khi không có |
| `config_time_source` | `measured_at` \| `received_at` \| null | Mốc thời gian dùng để chọn config: `measured_at` khi đã sync; fallback `received_at` khi `measured_at=null` (xấp xỉ — ghi nhận ngay trong bản ghi) |
| `components` | object/null | 6 thành phần của Sp tính theo config: `sh_m`, `sr_m`, `ss_m`, `c_m`, `zd_m`, `zr_m` (mét); `null` khi UNKNOWN |
| `config_fit_warnings` | array[string] | Cảnh báo cấu hình (không chặn, không giảm Sp), ví dụ Sp nằm ngoài vùng đo đã kiểm chứng `measurement_zone_m` |

### 1.2 Khối `prediction` (LSTM)

| Field | Kiểu | Ý nghĩa |
|-------|------|---------|
| `status` | `OK` \| `WARMUP` \| `INVALID_WINDOW` \| `MODEL_UNAVAILABLE` | Đủ 10 mẫu run liền mạch → OK; chưa đủ → WARMUP; mẫu lỗi/đứt run (sequence, gap, jitter) → INVALID_WINDOW; không nạp được artifact → MODEL_UNAVAILABLE. Thứ tự ưu tiên khi đánh giá: MODEL_UNAVAILABLE → INVALID_WINDOW → WARMUP → OK |
| `model_version` | string/null | Từ manifest artifact (vd `lstm-v1`); `null` khi `status != OK` |
| `horizons_ms` | array[int] | Luôn từ `configs/model.yaml`: `[100, 200, 300, 400, 500]` |
| `distance_m` | array[5 number]/null | 5 dự báo khoảng cách (mét, giữ float đầy đủ) tương ứng `horizons_ms`; `null` khi `status != OK` |
| `early_warning` | bool/null | `true` iff hiện tại còn CLEAR (`distance_m > Sp`) **và** min dự báo `<= Sp`; hiện tại đã VIOLATION → `false` (đã vi phạm, không phải cảnh báo sớm); `null` khi `status != OK`; Sp không xác định (UNKNOWN) → `false`, không bịa ngưỡng thay thế |
| `stale` | bool | Mode live: tuổi mẫu `now − measured_at > stale_after_s` (0.5 s) → `true`; `measured_at=null` → `false` (không đoán tuổi); mode offline → luôn `false`. Ghi chú: khi không có model artifact, khối prediction do service dựng fallback (MODEL_UNAVAILABLE) với `stale=false` cố định |
| `window_ref` | object/null | Nguồn 10 mẫu đầu vào: `device_id`, `boot_id`, `sequence_first`, `sequence_last`, `sample_count`, `max_age_ms` (tính từ monotonic thật của hai đầu cửa sổ; nhịp lý tưởng là 900ms); `null` khi `status != OK` |
| `mode` | `live` \| `offline` | `live`: tính lúc ingest (MQTT/ingest_jsonl/catch_up); `offline`: tính lại bởi replay |

LSTM bổ sung `preprocessing_version` (vd `pp-lstm-v1`) và
`predicted_violation_ms` (horizon đầu dự báo <= Sp, hoặc null). Cả hai null
khi prediction không OK. Các assessment cũ có thể thiếu hai field mở rộng.
So threshold trước mọi phép làm tròn; không dùng giá trị trình bày thay số gốc.

### 1.3 Ví dụ (shape thật, giá trị minh họa)

```json
{
  "schema_version": "1.0",
  "sample_ref": {"device_id": "esp32_01", "boot_id": "b1", "sequence": 125},
  "session_id": "run_001",
  "measured_at": "2026-09-21T12:00:12.500Z",
  "computed_at": "2026-09-21T12:00:12.531Z",
  "received_at": "2026-09-21T12:00:12.529Z",
  "distance_m": 0.643,
  "safety": {
    "status": "CLEAR",
    "sensor_valid": true,
    "assessment_valid": true,
    "sp_m": 0.44,
    "margin_m": 0.203,
    "config_version": "demo-v1",
    "config_time_source": "measured_at",
    "components": {"sh_m": 0.3, "sr_m": 0.02, "ss_m": 0.02, "c_m": 0.05, "zd_m": 0.03, "zr_m": 0.02},
    "config_fit_warnings": []
  },
  "prediction": {
    "status": "OK",
    "model_version": "lstm-v1",
    "horizons_ms": [100, 200, 300, 400, 500],
    "distance_m": [0.601, 0.562, 0.518, 0.478, 0.435],
    "early_warning": true,
    "stale": false,
    "window_ref": {"device_id": "esp32_01", "boot_id": "b1", "sequence_first": 116, "sequence_last": 125, "sample_count": 10, "max_age_ms": 900.0},
    "mode": "live"
  }
}
```

Ví dụ trên nhất quán nội tại theo quy tắc: hiện tại 0.643 > Sp 0.44 (CLEAR)
và dự báo cuối 0.435 ≤ 0.44 → `early_warning = true`. Lấy bản ghi thật qua
các kênh ở mục 2. Lưu ý giá trị số trong DB giữ nguyên độ chính xác float
(ví dụ thực tế `sp_m` có thể là `0.44000000000000006`, `max_age_ms` là
`900.0000000000001`) — TV2 nên so sánh với dung sai, không so sánh bằng
chính xác.

## 2. Cách TV2 lấy dữ liệu

Ba kênh, tuỳ hạ tầng của TV2:

### 2a. REST — `GET /assessments` (iot_backend đang chạy)

```bash
# Mọi assessment của một session, bản live (mặc định processor_version=safety-v1)
curl 'http://localhost:8000/assessments?session_id=run_001&processor_version=safety-v1&limit=5000'

# Bản replay offline của cùng session
curl 'http://localhost:8000/assessments?session_id=run_001&processor_version=replay-safety-v1'

# Tra đúng một bản ghi theo khóa mẫu
curl 'http://localhost:8000/samples/esp32_01/b1/125?processor_version=safety-v1'
```

Tham số query của `/assessments` (theo `iot_backend/main.py`):

| Tham số | Mặc định | Ý nghĩa |
|---------|----------|---------|
| `session_id` | không có | Lọc theo session; gửi nhưng không tồn tại → HTTP 404; bỏ qua → lấy toàn bộ DB |
| `processor_version` | `safety-v1` | Chọn lần tính (live hoặc một version replay) |
| `limit` | 1000 | Số bản ghi tối đa, cho phép 1..100000; trả theo thứ tự `device_id, boot_id, sequence` |

Không có phân trang ngoài `limit` — với DB lớn, TV2 tăng `limit` hoặc đọc
trực tiếp SQLite (2c). Endpoint bổ trợ: `GET /sessions/{session_id}` trả
thông tin session (ranh giới `start/stop_sequence`, `boundary_source`,
`boot_change`, ...) gộp với thống kê từ `session_stats`: `sample_count`,
`late_count`, `gap_count`, `violation_count`, `clear_count`,
`unknown_count` (ba đếm cuối tính theo `safety.status` của
`processor_version` mà iot_backend đang chạy).

### 2b. Export qua session_cli (raw telemetry)

```bash
.venv/bin/python -m scripts.session_cli export --session-id run_001 \
    --format jsonl --out data/run_001.jsonl
.venv/bin/python -m scripts.session_cli export --session-id run_001 \
    --format csv --out data/run_001.csv
```

Option bắt buộc của subcommand `export`: `--session-id`, `--format
jsonl|csv`, `--out`; option chung đặt **trước** subcommand: `--db`,
`--safety-config`, `--device-id`, `--env-file`.

**Lưu ý quan trọng:** export này trả **payload telemetry thô 11 field**
(không phải assessment_v1) — dùng để đối chiếu/tái tính độc lập. Bản ghi
assessment chỉ có qua API (2a) hoặc đọc trực tiếp DB (2c); hiện chưa có
endpoint CSV riêng cho assessment.

### 2c. Đọc trực tiếp SQLite `processed_samples`

DB SQLite WAL tại `HRC_DB_PATH` (mặc định `data/hrc.db`); DDL duy nhất trong
`iot_backend/ingestion/schema.sql`. Bảng `processed_samples`:

```sql
CREATE TABLE IF NOT EXISTS processed_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
  boot_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  processor_version TEXT NOT NULL,
  computed_at TEXT NOT NULL,
  mode TEXT NOT NULL CHECK (mode IN ('live','offline')),
  result_json TEXT NOT NULL,          -- bản ghi assessment_v1 (mục 1)
  UNIQUE (device_id, boot_id, sequence, processor_version)
);
```

`result_json` là chuỗi JSON của bản ghi ở mục 1 (key đã sort). Đọc read-only
để không can thiệp DB của iot_backend:

```bash
.venv/bin/python - <<'PY'
import sqlite3, json
conn = sqlite3.connect("file:data/hrc.db?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
for row in conn.execute(
    "SELECT * FROM processed_samples WHERE processor_version=? "
    "ORDER BY device_id, boot_id, sequence LIMIT 100",
    ("safety-v1",),
):
    record = json.loads(row["result_json"])
    print(record["sample_ref"], record["safety"]["status"])
PY
```

## 3. Cam kết bất biến

Những gì TV2 được giả định an toàn khi tiêu thụ dữ liệu:

1. **Khóa mẫu** `(device_id, boot_id, sequence)` là định danh đo duy nhất
   trong một boot; kết hợp với `processor_version` thành khóa duy nhất của
   `processed_samples` — hai lần tính khác version không bao giờ đè nhau.
2. **Timestamps ghi một lần**: `measured_at`/`received_at` giữ nguyên như lúc
   ghi, không chỉnh sửa sau đó (catch_up/replay tái dùng `received_at` từ
   bảng raw); `computed_at` là thời điểm tính của lần tính tương ứng.
3. **Thứ tự `sequence`** là thứ tự đo trong boot; thứ tự nhận MQTT không dùng
   làm thứ tự đo (mẫu đến muộn được đánh dấu `is_late` trong `raw_samples`,
   không đổi sequence).
4. **`mode` và `processor_version` phân biệt nguồn lần tính**: `safety-v1` +
   `live` (tính lúc ingest), `replay-safety-v1` + `offline` (mặc định replay;
   có thể đặt tên version khác qua `--processor-version`). Ghi nhận là
   INSERT OR IGNORE — chạy lại cùng version không nhân bản, không ghi đè.

## 4. Giới hạn demo (những gì TV3/TV4 KHÔNG đảm bảo)

- **Không ghi Blockchain tức thì**: việc ghi lên chain là nhiệm vụ TV1–TV2;
  iot_backend chỉ lưu SQLite và phục vụ qua API/export.
- **Thiết bị chưa sync giờ**: `measured_at = null` (kèm `time_synced=false`);
  đánh giá an toàn khi đó dùng `received_at` làm mốc chọn config
  (`config_time_source = "received_at"`) — chỉ là xấp xỉ, không phải giờ đo.
- **Replay tạo bản ghi riêng, không sửa lịch sử**: mỗi lần replay ghi bản ghi
  `mode = "offline"` với `processor_version` riêng; bản ghi live của cùng mẫu
  giữ nguyên.
- **Boundary session**: MQTT commit raw trước; trong lúc `session_start`/`stop`
  chờ ACK, compute worker tạm chờ boundary gate. Mẫu đang bay được gán theo
  đúng boundary sau khi ACK, và stop fallback nhìn cả raw pending
  (`iot_backend/sessions/manager.py`, `iot_backend/ingestion/mqtt_listener.py`).
- **Chưa có endpoint CSV cho assessment** và không có phân trang sâu — DB
  lớn thì TV2 đọc SQLite trực tiếp (2c).

Xem thêm: quy trình thu và các lệnh CLI ở
[data-collection.md](data-collection.md); kịch bản tái lập ở
[experiments.md](experiments.md).

## 5. Event package v1

`GET /events?processor_version=safety-v1&session_id=run_001` trả các trigger;
`GET /events/{event_id}` trả gói theo
[schemas/event_package_v1.schema.json](../schemas/event_package_v1.schema.json).

- `VIOLATION`/`UNKNOWN` phát khi safety chuyển vào trạng thái đó trong run.
- `PREDICTED_WARNING` cần ít nhất 2 cảnh báo trong 3 mẫu liên tiếp, safety
  hiện tại CLEAR và prediction OK không stale. Giữ assessment của cảnh báo
  đầu tiên (`trigger_ref`), cùng mẫu xác nhận (`confirmed_ref`). Ba mẫu không
  warning liên tiếp rearm; gap/jitter/config/model/session/boot đổi cắt state.
- `event_id` deterministic theo processor/scope/type/trigger sequence. State,
  event và processed sample commit cùng transaction; restart không nhân bản.
- `raw_samples` giữ telemetry gốc ít nhất 2s trước/1s sau trigger theo monotonic,
  kèm mẫu chặn biên gần nhất khi timestamp không trùng biên do jitter,
  chỉ cùng device/boot/session và processor version. Gói không bịa mẫu thiếu.
- `package_status=collecting` khi nguồn chưa đi hết post window;
  `ready` khi đã đi hết hoặc session đóng. Ready vẫn có thể thiếu dữ liệu:
  đọc `coverage.complete`, `pre/post_complete`, available_ms và gap counts.
- `/events` là danh sách trigger gọn; coverage/raw nằm ở endpoint chi tiết,
  được đọc từ dữ liệu đã lưu. Mẫu tới muộn có thể lấp gap raw, nhưng không
  quay ngược detector live để tạo cảnh báo.

Đây là gói đầu vào tối thiểu cho bộ gom TV2; không phải blockchain outbox
đã gửi lên chain. TV2 tự canonicalize/hash, định nghĩa chính sách tiêu thụ và
xử lý các gói incomplete. Các field `computed_at` và model/config version
được giữ trong assessment đầu tiên.
