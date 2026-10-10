# Thu dataset khi iot_backend chạy nền

Tài liệu này mô tả cách thu dữ liệu khi iot_backend và ESP32 chạy liên tục ở nền,
đặc biệt là cách hệ thống biết mỗi đoạn dữ liệu thuộc kịch bản nào.

## Nguyên tắc chính

ESP32 **không tự nhận diện scenario**. Firmware chỉ đo HC-SR04 và gửi telemetry
qua MQTT:

```text
ESP32 → MQTT telemetry → iot_backend → raw_samples
```

Scenario là metadata do người vận hành hoặc script điều khiển khai báo khi mở
một `session`. Backend lưu metadata đó vào bảng `sessions`, rồi gán các mẫu
telemetry vào session bằng:

```text
(device_id, boot_id, sequence)
```

Vì vậy, iot_backend chạy nền không cần đoán rằng một đoạn dữ liệu là `approach`,
`recede` hay `stationary`. Kịch bản được xác định bởi session mà người vận hành
đã mở trước khi thực hiện thao tác.

Luồng đầy đủ:

```text
iot_backend chạy nền
  → ESP32 gửi telemetry liên tục
  → mở session với scenario cụ thể
  → thực hiện một kịch bản duy nhất
  → đóng session
  → export session thành dataset
```

Không nên đặt nhiều kịch bản khác nhau trong cùng một session. Một session nên
là một lượt thí nghiệm liên tục, có một scenario, một target type và một bộ
metadata rõ ràng.

## Scenario được lưu ở đâu?

Telemetry gửi trên MQTT không chứa `scenario` vì
`schemas/telemetry_v1.schema.json` chỉ mô tả dữ liệu đo. Một session được mở
với các trường:

```json
{
  "session_id": "approach_slow_001",
  "scenario": "approach",
  "object": "person",
  "zone_note": "0.9 m to 0.3 m, slow"
}
```

Backend lưu các trường này trong bảng `sessions`:

```text
session_id = approach_slow_001
scenario   = approach
object     = person
status     = running
```

Khi export bằng `scripts/export_dataset.py`, iot_backend sao chép metadata session
vào từng dòng dataset:

```json
{
  "device_id": "esp32_01",
  "boot_id": "boot-001",
  "sequence": 125,
  "distance_mm": 637,
  "valid": true,
  "session_label": "approach_slow_001",
  "scenario": "approach",
  "target_type": "person",
  "synthetic": false
}
```

`target_type` trong dataset được lấy từ trường `object` của session. Vì vậy khi
mở session cần truyền `object` rõ ràng, ví dụ `person`, `flat_board` hoặc
`hand`.

## Thu dữ liệu thật với iot_backend Docker chạy nền

Khởi động iot_backend một lần và để nó chạy trong suốt quá trình thu:

```bash
docker compose up -d --build --wait mqtt iot_backend
docker compose ps
curl http://localhost:8001/health
docker compose logs -f --tail=100 mqtt iot_backend
```

ESP32 cần trỏ MQTT tới IP LAN của máy chạy Docker:

```c
#define HRC_MQTT_URI "mqtt://<IP_MAY_CHAY_DOCKER>:1885"
```

`localhost` trên ESP32 không trỏ tới máy chạy Docker. Backend trong Compose
kết nối tới `mqtt:1883`, còn ESP32 bên ngoài Docker dùng cổng host `1885`.

Sau khi ESP32 đã kết nối và bắt đầu gửi mẫu, mở session bằng API. Dùng API khi
iot_backend Docker đang chạy để chỉ có iot_backend ghi SQLite:

```bash
curl -X POST http://localhost:8001/sessions/start \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"approach_slow_001","scenario":"approach","object":"person","zone_note":"0.9 m to 0.3 m, slow"}'
```

Sau đó thực hiện đúng một kịch bản `approach` chậm. Khi kết thúc, đóng session:

```bash
curl -X POST \
  http://localhost:8001/sessions/approach_slow_001/stop
```

Kiểm tra metadata và số mẫu:

```bash
curl http://localhost:8001/sessions/approach_slow_001
```

Lặp lại quy trình với session ID và scenario khác:

```bash
# Session tiếp theo: rời xa ở tốc độ vừa
curl -X POST http://localhost:8001/sessions/start \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"recede_medium_001","scenario":"recede","object":"person","zone_note":"0.3 m to 0.9 m, medium"}'

# Thực hiện kịch bản recede medium
curl -X POST \
  http://localhost:8001/sessions/recede_medium_001/stop
```

Một session đang chạy chỉ thuộc về device đã cấu hình. Backend dùng ack của
thiết bị để chốt sequence bắt đầu/kết thúc. Nếu không nhận được ack, iot_backend
sử dụng DB fallback và ghi `boundary_source = db_fallback`.

## Khi nào mẫu được gán vào session?

Có ba trường hợp:

| Tình huống | Kết quả |
|---|---|
| Mẫu đến sau boundary start và trước boundary stop | Được gán vào session |
| Mẫu đến trước khi mở session | Vẫn lưu raw, nhưng không thuộc session mới |
| `boot_id` đổi giữa session | Không gán boot mới vào session cũ; đặt `boot_change=1` |

Mẫu chưa thuộc session không bị xóa. Nó vẫn nằm trong `raw_samples`, nhưng
không được `export_dataset.py` lấy vào dataset theo session.

Nếu worker compute xử lý chậm, raw vẫn được commit trước. Khi start/stop
session, boundary condition bảo đảm mẫu đang xử lý không bị gán sai qua ranh
giới.

## Thu liên tiếp nhiều session bằng tay

Với dữ liệu thật, người vận hành thường cần chuyển kịch bản sau mỗi lượt:

```text
start approach_slow_001
→ thực hiện tiến gần chậm
→ stop approach_slow_001

start approach_fast_001
→ thực hiện tiến gần nhanh
→ stop approach_fast_001

start recede_medium_001
→ thực hiện rời xa tốc độ vừa
→ stop recede_medium_001
```

Nên dùng tên session chứa scenario và tốc độ để dễ truy vết:

```text
stationary_044_board_001
approach_slow_person_001
approach_fast_person_001
recede_medium_person_001
stop_before_sp_045_001
oscillation_near_sp_001
sensor_error_timeout_001
```

Metadata `scenario` vẫn phải truyền riêng trong request start; không dựa vào
việc đặt tên session để iot_backend tự phân loại.

## Chạy simulator ở nền

Simulator có hai ý nghĩa khác nhau:

- `--scenario` quyết định quỹ đạo synthetic mà simulator sinh ra.
- `scenario` trong lệnh mở session là metadata iot_backend dùng để phân loại dữ liệu.

Hai giá trị này phải khớp. Ví dụ chạy simulator nền với Docker:

```bash
python -m scripts.device_sim \
  --host localhost --port 1885 \
  --device-id esp32_01 \
  --samples 200 \
  --scenario approach \
  --record-jsonl data/sim-approach-001.jsonl &

SIM_PID=$!
sleep 1

curl -X POST http://localhost:8001/sessions/start \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"sim_approach_001","scenario":"approach","object":"person"}'

wait "$SIM_PID"

curl -X POST http://localhost:8001/sessions/sim_approach_001/stop
```

Với simulator, nên phát một vài mẫu trước khi mở session để thiết bị có thể
trả ack sequence. Nếu start trước khi simulator có mẫu đầu tiên, iot_backend vẫn có
fallback nhưng boundary có thể không dùng được device ack.

Simulator dùng `boot_id` dạng `synthetic_*` và recording có
`synthetic=true`. Không đưa DB simulator vào dataset real:

```bash
docker compose exec -T iot_backend python -m scripts.export_dataset \
  --db /app/data/hrc.db \
  --out /app/data/synthetic-sessions.jsonl \
  --source synthetic
```

## Export dataset sau khi thu

Chỉ export sau khi mọi session cần dùng đã đóng:

```bash
docker compose exec -T iot_backend python -m scripts.export_dataset \
  --db /app/data/hrc.db \
  --out /app/data/real-sessions-pilot.jsonl \
  --source real

docker compose cp iot_backend:/app/data/real-sessions-pilot.jsonl \
  data/real-sessions-pilot.jsonl
```

Kiểm tra metadata và số lượng session:

```bash
wc -l data/real-sessions-pilot.jsonl
python3 - <<'PY'
import json
from pathlib import Path

path = Path("data/real-sessions-pilot.jsonl")
rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
print("rows:", len(rows))
print("sessions:", sorted({row.get("session_label") for row in rows}))
print("scenarios:", sorted({row.get("scenario") for row in rows}))
print("synthetic:", sorted({row.get("synthetic") for row in rows}))
PY
```

Nếu dùng `--source real`, tool từ chối boot ID có dấu hiệu simulator như
`synthetic_*` hoặc `sim_*`. Không đổi nhãn thủ công để vượt qua kiểm tra này.

## Điều kiện trước khi train LSTM

Dataset phải có nhiều session, không chỉ nhiều dòng. Pipeline chia session trước
khi tạo cửa sổ để tránh leakage. Một file có 1 session có thể tạo được cửa sổ,
nhưng không đủ để tạo train/validation/test có ý nghĩa.

Trước khi train cần kiểm tra:

- mỗi session chỉ thuộc một scenario;
- có đủ các nhóm `stationary`, `approach`, `recede`, `stop`, `oscillation` và lỗi;
- có cả mẫu CLEAR và các đoạn crossing qua `Sp`;
- `boot_id` ổn định trong session;
- sequence liên tục và chu kỳ gần 100 ms;
- mẫu lỗi vẫn được giữ trong raw nhưng bị loại khi tạo window;
- `synthetic=false` với dataset đo thật;
- train/validation/test được chia theo session, không chia theo dòng.

Mục tiêu pilot là khoảng 50–80 session. Sau khi quy trình ổn định, có thể mở
rộng lên bộ dữ liệu chính theo kế hoạch TV4. Nếu validation hoặc test không có
session/window, trainer phải dừng và yêu cầu thu thêm dữ liệu.

## Các lỗi vận hành thường gặp

### Backend không biết scenario

Kiểm tra request `/sessions/start` có truyền `scenario` và `object` hay chưa.
Telemetry MQTT không chứa scenario; đó là thiết kế hiện tại.

### Mẫu có trong raw nhưng không có trong dataset

Kiểm tra session đã được mở trước thời điểm thu chưa, session đã đóng chưa,
`device_id` và `boot_id` có khớp không, và session có `boot_change=1` không.

### Nhiều scenario bị trộn

Không dùng một session cho nhiều kịch bản. Đóng session hiện tại rồi mở session
mới với scenario mới.

### Dataset synthetic bị gắn nhãn real

Tách DB synthetic khỏi DB đo thật. Dùng `--source synthetic` khi export và giữ
`synthetic=true` trong recording.

### Muốn tự động chạy nhiều kịch bản

Repository có runner cho phần cứng thật. Chạy từ repo root:

```bash
.venv/bin/python -m scripts.collect_dataset

# Hoặc các lượt định nghĩa trước: sửa target và thao tác theo phần cứng thực.
.venv/bin/python -m scripts.collect_dataset \
  --plan configs/collection.example.yaml
```

Runner hỏi nhãn và chờ Enter trước START. Mặc định chờ ACK ESP32, rồi thu đến
Enter STOP hoặc hết duration. Mỗi lượt có raw export, labelled JSONL và quality
trong manifest; speed_level được bảo toàn cho train/evaluation. Hướng dẫn đầy
đủ ở [data-collection.md](data-collection.md#runner-gắn-nhãn-bằng-tín-hiệu-startstop).

Runner thực hiện chuỗi:

```text
POST /sessions/start với scenario A
→ chờ thời lượng hoặc tín hiệu kết thúc
→ POST /sessions/{id}/stop
→ POST /sessions/start với scenario B
→ ...
```

Không nên để runner cố suy luận scenario từ đường khoảng cách; metadata cần được
chọn từ kịch bản mà người vận hành thực sự đang thực hiện.
