# Quy trình thu dữ liệu HRC Safety Log

Ba cách thu dữ liệu: (1) thiết bị thật qua MQTT, (2) dataset synthetic không cần
phần cứng, (3) device simulator synthetic qua MQTT. Sau đó nạp/replay/export
bằng các CLI trong `scripts/`. Các ví dụ venv dưới đây dành cho Python chạy
trực tiếp. Với Docker, API ở cổng **8001** và DB trong named volume;
[docker.md](docker.md) có lệnh session/export/AI tương ứng. Dùng API để
điều khiển iot_backend Docker đang chạy.

## 1. Quy ước session

- Ranh giới session chốt theo **ack thiết bị** cho lệnh `session_start`/
  `session_stop` (`boundary_source = device_ack`): mẫu đầu session là
  `ack.sequence + 1` tại lệnh start; lệnh stop nằm trong session
  (`[start_sequence, stop_sequence]`, `stop_sequence = ack.sequence` của lệnh
  stop). Không gán theo giờ nhận MQTT.
- **Fallback** khi không nhận được ack trong `HRC_ACK_TIMEOUT_S` (mặc định 8 s)
  hoặc chạy CLI `--no-mqtt`: ranh giới start = sequence lớn nhất đã nhận của
  boot hiện hành tại thời điểm start (`boundary_source = db_fallback`).
- Boot restart giữa session (`boot_id` đổi): mẫu của boot mới **không gán** vào
  session, session bị đánh dấu `boot_change = 1`.
- Bảng `sessions` lưu `scenario`, `object`, `zone_note`, `start_boot_id`,
  `start_sequence`, `stop_sequence` để truy vết.
- Cảnh báo vận hành: không chạy `session_cli --mqtt` song song với iot_backend trên
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

Tương đương qua API iot_backend đang chạy:

```bash
curl -X POST http://localhost:8000/sessions/start \
     -H 'Content-Type: application/json' -d '{"scenario": "demo", "session_id": "run_001"}'
curl -X POST http://localhost:8000/sessions/run_001/stop
```

## 3. Thu với thiết bị thật

### Runner gắn nhãn bằng tín hiệu START/STOP

Khi iot_backend Docker đang chạy, dùng runner để mỗi lượt có đúng một nhãn và hai
boundary control gửi xuống ESP32. Runner không mở SQLite và không chạy một
`MqttListener` thứ hai; iot_backend hiện tại là tiến trình duy nhất ghi DB.

Chạy interactive mode:

```bash
.venv/bin/python -m scripts.collect_dataset \
  --output-dir data/datasets/real-20261008
```

Mỗi lượt runner hỏi scenario, target type/object, tốc độ và ghi chú. Sau khi
bạn đặt mục tiêu, nhấn Enter lần thứ nhất để runner gọi
`POST /sessions/start`. Backend gửi `session_start` QoS1 tới topic control;
ESP32 trả ACK chứa `boot_id` và `sequence`. Runner mặc định chỉ cho thu sau
ACK START hợp lệ; nếu chưa có ACK thì đóng session vừa mở và báo lỗi.
Trong chế độ menu, nếu API xác nhận session lỗi đã đóng, runner giữ lượt lỗi
trong manifest và cho chọn lượt mới trong cùng collection. Nếu START/STOP
bị timeout HTTP hoặc chưa xác nhận đóng session, runner dừng với
`needs_reconciliation`; kiểm tra trạng thái qua API trước khi thu tiếp. Chọn
`--allow-db-fallback` nếu muốn dùng ranh giới DB; lượt sẽ được đánh dấu WARN.
Sau khi START được xác nhận, runner báo để bạn thực hiện động tác. Khi xong, nhấn Enter lần thứ hai;
runner gọi `POST /sessions/<id>/stop`, iot_backend gửi `session_stop`, rồi runner
chờ telemetry đang truyền dở trước khi export.

Mỗi lượt tạo một file `<session_id>.jsonl`. File `dataset.jsonl` là phần gộp
của các lượt trong lần chạy, còn `manifest.json` lưu nhãn, boundary, số mẫu,
sequence thiếu, mẫu invalid, nhịp đo và SHA-256. File `<session_id>.raw.jsonl`
giữ bản export trước khi thêm nhãn. `manifest.json` còn giữ tốc độ, ghi chú và
trạng thái request để khôi phục khi mất kết nối. Mỗi dòng dataset có thêm:
`session_label`, `scenario`, `target_type`, `speed_level` và
`synthetic=false`; payload telemetry 11 trường vẫn giữ nguyên.

Có thể khai báo nhiều lượt trong YAML. `repetitions` được mở rộng thành các
lượt độc lập; runner vẫn dừng trước mỗi START để người vận hành chuẩn bị:

```bash
.venv/bin/python -m scripts.collect_dataset \
  --plan configs/collection.example.yaml \
  --output-dir data/datasets/real-20261008
```

Ví dụ một lượt tự dừng sau 30 giây, phù hợp chạy không tương tác:

```bash
.venv/bin/python -m scripts.collect_dataset \
  --scenario approach --target-type person --speed-level slow \
  --duration-s 30 --non-interactive \
  --output-dir data/datasets/real-20261008
```

`--non-interactive` bắt buộc có `--duration-s` hoặc plan mà mọi entry đều có
`duration_s`. Không retry POST START/STOP sau timeout mạng vì request có thể đã
được iot_backend nhận; kiểm tra `GET /sessions` và `manifest.json` trước khi xử lý
lại. Ctrl+C trong lúc thực hiện động tác sẽ gửi STOP một lần, xuất dữ liệu
đang có và đánh dấu lượt bị ngắt. Nếu bị ngắt trong lúc request chưa được xác
nhận, runner giữ trạng thái `needs_reconciliation` trong manifest.

Nếu lượt đầu PASS nhưng lượt sau báo thiếu ACK cùng log firmware
`telemetry enqueue loss: ESP_ERR_NO_MEM`, xem `pending`, `outbox` và `pubacks`.
`wifi=1 mqtt=1 control=1` vẫn có thể đi kèm backlog gửi: ACK START/STOP phải
chờ telemetry trong FIFO SDK. Firmware dùng publish trực tiếp trong worker
để tránh cơ chế này; cần build/flash bản mới trước khi thử lại nhiều lượt.
`HRC_ACK_TIMEOUT_S=8.0` là giới hạn chờ, không xử lý được backlog tăng liên tục.

Mỗi lần chạy nên dùng một thư mục output mới. Khi bỏ `--output-dir`, runner
tạo thư mục riêng dưới `data/datasets/`. Nếu thư mục chỉ định đã có dữ liệu,
runner báo lỗi để tránh ghi đè manifest hoặc nối lẫn các lần chạy.

API hiện chỉ công khai `boundary_source` cho START; manifest ghi nguồn STOP là
`not_exposed_by_api`, không suy diễn rằng STOP luôn có ACK thiết bị.

Chất lượng lượt là `PASS` khi schema, boot, boundary, sequence và nhịp mẫu hợp
lệ. Nhịp đo được so với `configs/model.yaml`: `delta_s`, `max_time_jitter_s`
và `max_gap_s`. Nếu thiếu mẫu, boot đổi hoặc có invalid, runner lưu dữ liệu và
đánh dấu `WARN`. JSON schema sai, sample ngoài boundary hoặc boot simulator
được giữ trong raw export và không được gắn nhãn đo thật.

`dataset.jsonl` chứa cả lượt PASS và WARN. Trước train, kiểm tra manifest, chọn
các lượt phù hợp và giữ mẫu invalid để pipeline cắt window tại lỗi. Tốc độ
slow/medium/fast là nhãn người vận hành khai báo; chưa phải vận tốc hiệu chuẩn.

### Dataset cần bổ sung cho đánh giá LSTM

Dataset hiện tại đủ để làm báo cáo dự báo khoảng cách ban đầu, nhưng chưa đủ
để xác nhận F1 cảnh báo sớm: test chính không có crossing; split có crossing
chỉ có 3 sự kiện và là split thăm dò. Thu một đợt mới vào ngày/buổi khác, đặt
nhãn trước khi xem kết quả, giữ nguyên toàn bộ session làm test cuối và không
đổi seed để tìm split có F1 đẹp. Không gộp các cửa sổ 100 ms chồng lấn thành số
đếm sự kiện độc lập.

Ma trận tối thiểu đề xuất cho báo cáo thử nghiệm tiếp theo:

| Kịch bản | Số lượt độc lập | Điều kiện cần có |
|---|---:|---|
| Approach vượt Sp | 20 | Bắt đầu CLEAR, tiến qua Sp ở slow/medium/fast; ghi thời điểm crossing và khoảng cách tham chiếu. Đây là positive event chính cho recall/F1. |
| Approach dừng trước Sp | 10 | Bắt đầu ngoài Sp, giảm tốc và dừng an toàn trên ngưỡng; dùng để kiểm false warning. |
| Stationary an toàn | 10 | Giữ vật ổn định rõ ràng trên Sp, gồm các khoảng cách gần/xa và thời gian đủ dài để chạy debounce. |
| Oscillation quanh Sp | 10 | Dao động có kiểm soát hai phía ngưỡng, ghi chuyển trạng thái và số crossing độc lập; đánh giá cảnh báo lặp/chattering. |
| Recede | 5 | Bắt đầu gần rồi đi xa, kiểm tra không cảnh báo nhầm chiều chuyển động. |
| Sensor invalid/fault | 5 lượt cho mỗi mã lỗi quan sát được | Giữ `valid=false` và `error_code` thật; không gán khoảng cách giả. Mẫu lỗi hiện bị cắt khỏi LSTM windows, nên nhóm này đánh giá UNKNOWN/ingestion, không phải F1 crossing của model. |

Mỗi lượt nên khoảng 30–45 giây, telemetry 10 Hz, một session cho một lần lặp,
boundary START/STOP được ACK và chất lượng PASS. Phân bố test đề xuất có ít nhất
20 crossing độc lập và 20 phiên âm tính không crossing; tổng số window không
thay thế số sự kiện. Nếu có nhiều thiết bị/ngày đo, giữ trọn thiết bị hoặc ngày
chưa thấy trong train để kiểm tra tổng quát hóa. Nếu chỉ có một thiết bị, giới
hạn kết luận vào các buổi đo của thiết bị đó.

Đo khoảng cách thật ở các mốc quanh Sp bằng thước/laser hoặc video đã hiệu chuẩn;
ghi mốc crossing độc lập với HC-SR04 để tránh dùng chính tín hiệu model dự báo
làm ground truth. Thử nghiệm chuyển động phải có người giám sát, vật thể di
chuyển chậm và vùng thử an toàn; không chủ động đưa người vào vùng va chạm của
robot đang hoạt động. Thu lỗi cảm biến bằng thao tác kiểm thử có kiểm soát hoặc
log lỗi tự nhiên; không rút dây/cấp nguồn gây hỏng phần cứng.

LSTM hiện dự báo khoảng cách, không phân loại sensor fault. Không nhập lỗi mô
phỏng vào dataset real; nếu cần kiểm tra fault detection, xây evaluator riêng
cho trạng thái `UNKNOWN` theo `error_code`. Báo cáo chính nên tính regression
MAE/RMSE từng horizon, event precision/recall/F1 sau detector debounce 2/3,
miss/false-warning rate, lead time, confusion/support theo session, và kèm
khoảng cách tham chiếu. Giữ test set bất biến cho đến khi chốt model.

### Thu bằng API hoặc CLI hiện có

1. Khởi động hệ thống theo [wiring.md](wiring.md): broker → iot_backend → firmware.
2. Mở session: `session_cli start ... --mqtt` (hoặc POST `/sessions/start`).
3. Thực hiện kịch bản di chuyển vật/người quanh robot trong thời gian thu.
4. Đóng session: `session_cli stop --session-id <id>` (hoặc POST stop).
5. Export JSONL/CSV: lệnh ở mục 2, hoặc
   `curl -o data/run_001.jsonl 'http://localhost:8000/sessions/run_001/export?format=jsonl'`.

Telemetry 10 Hz tự ingest khi iot_backend đang chạy (`HRC_MQTT_ENABLED=true`); safety
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

`scripts/device_sim.py` phát telemetry QoS1, nhận control và ack sequence
mới nhất. Mặc định device `esp32_sim`, boot `synthetic_*`; chạy trên DB demo
riêng để không lẫn phép đo thật.

```bash
.venv/bin/python -m scripts.device_sim --host localhost --port 1883 \
  --samples 200 --scenario approach --record-jsonl data/simulator.jsonl
# Không cần broker: JSONL có nhãn để train/smoke
.venv/bin/python -m scripts.device_sim --dry-run --samples 100 > data/simulator-dry.jsonl
```

Để session được ack, `--device-id` của simulator phải trùng device iot_backend
đang điều khiển (`DEVICE_ID` khi chạy trực tiếp, `HRC_DEVICE_ID` trong Compose).
Ví dụ với iot_backend mặc định, thêm `--device-id esp32_01`; với Docker host,
thay port bằng **1885**, API bằng **8001**. Tách stack/DB synthetic khỏi DB
thu số đo thật.

MQTT payload giữ 11 field telemetry theo schema; synthetic/scenario/session
label nằm trong recording JSONL và danh tính boot simulator, không thêm vào
wire payload. Session điều khiển qua API iot_backend như mục 2. Tạo session sau
khi simulator đã phát mẫu đầu để ack có sequence >=1.

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

## 7. Export dataset cho LSTM và hiệu chuẩn

Đóng toàn bộ session trước khi xuất:

```bash
.venv/bin/python -m scripts.export_dataset --db data/hrc.db \
  --out data/closed-sessions.jsonl --source real
```

Chọn `--source synthetic` với simulator, giữ DB/dataset riêng. Tool thêm
session_label, scenario và target_type (từ `sessions.object`) trên từng dòng,
loại mẫu không được gán session, giữ mẫu lỗi để windowing thống kê drop.
Training không nhận nhiều raw file thiếu session boundary. Dùng đủ session
để mỗi train/val/test có dữ liệu; pipeline sẽ báo lỗi nếu split không có cửa sổ.
Xem [lstm.md](lstm.md) và [calibration.md](calibration.md).
