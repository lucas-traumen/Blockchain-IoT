# Vận hành MQTT, backend và AI bằng Docker

Chạy từ repo root. Compose project mặc định `blockchain-hrc` để quản lý riêng
container/volume của repo này. Không dùng broker/API của dự án khác đang
chiếm cổng 1883/1884/8000 trên máy phát triển.

## Khởi động

```bash
docker compose up -d --build --wait mqtt backend
docker compose ps
curl http://localhost:8001/health
```

| Service | Vai trò | Địa chỉ trên host | Dữ liệu |
|---|---|---|---|
| `mqtt` | Mosquitto 2.0.22 | `mqtt://<IP-LAN>:1885` | Volume `mqtt_data` |
| `backend` | FastAPI, ingestion, safety, inference, events | `http://localhost:8001` | SQLite `/app/data/hrc.db` trong `backend_data` |
| `ai` | Công cụ TensorFlow 2.20.0/Keras 3.15.1, profile tùy chọn | Không mở cổng riêng | Bind `data/`, `ai_model/artifacts/` |

Backend dùng `mqtt:1883` trong mạng Compose. ESP32 dùng **IP LAN của máy
Docker và cổng host 1885**. Tại lần kiểm tra ngày 2026-10-02, máy này có IP
`192.168.100.3`; đặt macro trong
`iot_code/esp32/main/network_config.local.h`:

```c
#define HRC_WIFI_LIST_MODE 1
#define HRC_WIFI_NETWORKS { \
    {"WiFi_cua_ban", "mat_khau_wifi"}, \
    {"Hotspot", "mat_khau_hotspot"}, \
}
#define HRC_MQTT_URI "mqtt://192.168.100.3:1885"
```

ESP32 tự quét, ưu tiên RSSI mạnh nhất trong danh sách, thử mạng tiếp theo
khi thất bại. Macro mode bỏ qua NVS, không cần nhập SSID/password trên serial.
Sửa macro rồi build/flash lại; xem [firmware README](../iot_code/esp32/README.md).

Sửa macro broker và build/flash lại khi máy chuyển mạng/IP. `localhost` trên ESP32 không trỏ về
máy chạy Docker. Cổng MQTT hiện dùng listener LAN và anonymous, không TLS/auth,
đúng chế độ demo của firmware/backend.

`/health` báo `status=ok`, DB/model đang dùng. `mqtt=true` báo listener được
bật; kiểm tra telemetry/session ack mới xác nhận luồng broker thực sự hoạt
động. `model_available=false` khi chưa có artifact: prediction trả
`MODEL_UNAVAILABLE`, safety vẫn tính và lưu raw.

## Cấu hình cổng và runtime

Mặc định có thể chạy ngay. Để đổi cấu hình:

```bash
cp compose.env.example compose.env
# Sửa HRC_* trong compose.env theo máy của bạn.
docker compose --env-file compose.env up -d --build --wait mqtt backend
```

Dùng cùng `--env-file compose.env` cho những lệnh Compose tiếp theo. Các key:

- `HRC_MQTT_HOST_PORT=1885`, `HRC_API_HOST_PORT=8001`.
- `HRC_MQTT_BIND=0.0.0.0`, `HRC_API_BIND=0.0.0.0`.
- `HRC_DEVICE_ID=esp32_01`: khớp firmware hoặc simulator khi thử session.
- `HRC_RUNTIME_TARGET=backend`, `HRC_MODEL_TYPE=rf`, `HRC_MODEL_SUBDIR=`:
  backend RF không có TensorFlow, artifact RF trực tiếp trong
  `ai_model/artifacts/`. Có thể đặt subdirectory nếu lưu artifact riêng.
- `HRC_RUNTIME_TARGET=ai`, `HRC_MODEL_TYPE=lstm`,
  `HRC_MODEL_SUBDIR=lstm-v1`: dùng image có TensorFlow với artifact đầy đủ
  trong `ai_model/artifacts/lstm-v1/`.
- `HRC_AI_UID`/`HRC_AI_GID`: UID/GID máy host (`id -u`, `id -g`) để công cụ AI
  ghi dataset/artifact với quyền đúng. Backend chạy bằng UID 10001 và đọc
  configs/artifacts; các file cần có quyền đọc cho UID này.

`.env` dùng cho Python chạy trực tiếp; các key `MQTT_HOST`/`MQTT_PORT` trong
file đó không thay địa chỉ service nội bộ đã khai báo trong Compose.
`compose.env` không commit và không đưa vào image.

## AI và model

AI là công cụ chạy theo lệnh, không khởi động thêm API hoặc tiến trình ghi DB:

```bash
docker compose --profile ai build ai

docker compose run --rm ai python -m scripts.synthetic_dataset \
  --out data/sim.jsonl --sessions 30 --samples-per-session 200 \
  --seed 42 --scenario all

docker compose run --rm ai python -m ai_model.lstm_train \
  --dataset data/sim.jsonl --out-dir ai_model/artifacts/lstm-v1

docker compose run --rm ai python -m ai_model.evaluate --model-type lstm \
  --dataset data/sim.jsonl --model-dir ai_model/artifacts/lstm-v1 \
  --output data/lstm-report.json
```

Sau khi có artifact được kiểm tra, chọn ba biến LSTM trong `compose.env` rồi
chạy lại lệnh `up --build --wait` ở trên. Dataset ví dụ là **synthetic**;
train chạy được không chứng minh model đạt chất lượng trên HC-SR04 thật.
Xem [lstm.md](lstm.md) cho artifact, split và so baseline.

## Session và export

Backend là tiến trình điều khiển session và ghi DB. Dùng API:

```bash
curl -X POST http://localhost:8001/sessions/start \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"run_001","scenario":"approach","object":"hop"}'
curl -X POST http://localhost:8001/sessions/run_001/stop
curl http://localhost:8001/sessions/run_001
mkdir -p data
curl -o data/run_001.jsonl \
  'http://localhost:8001/sessions/run_001/export?format=jsonl'
```

Export dataset các session đã đóng bằng tool hiện có rồi copy ra host:

```bash
docker compose exec -T backend python -m scripts.export_dataset \
  --db /app/data/hrc.db --out /app/data/measured-sessions.jsonl --source real
docker compose cp backend:/app/data/measured-sessions.jsonl data/measured-sessions.jsonl
```

Tool từ chối `--source real` nếu DB có boot synthetic/simulator. DB thử nghiệm
phải export `--source synthetic`; giữ nguồn dữ liệu rõ ràng.
Không chạy `scripts.session_cli --mqtt`, replay hoặc ingester khác song song
để sửa cùng DB backend đang dùng.

Simulator cần `--device-id` trùng `HRC_DEVICE_ID` để START/STOP được ack.
Ví dụ dưới đây phát **synthetic** vào DB stack hiện tại, chỉ dùng cho stack
thử nghiệm:

```bash
docker compose run --rm --no-deps backend python -m scripts.device_sim \
  --host mqtt --port 1883 --device-id esp32_01 --samples 200 --scenario approach
```

## Log, restart và giữ dữ liệu

```bash
docker compose logs -f --tail=100 mqtt backend
docker compose restart mqtt backend
docker compose up -d --wait mqtt backend
docker compose down
```

`down` giữ named volume; lần `up` sau dùng lại DB/dữ liệu broker.
Build/recreate image giữ volume. Xóa volume sẽ xóa dữ liệu.
Artifact/config mount chỉ đọc ở backend; sau khi thay model/config, recreate
backend để nạp lại. ESP32/MQTT clients phải reconnect về đúng IP/cổng.

Ngày 2026-10-02 đã kiểm tra: hai service healthy, 50 telemetry synthetic/boot
được lưu, START/STOP dùng `device_ack`; SQLite và MQTT retained test tồn tại
qua restart, và pipeline tiếp tục nhận sau restart. Image AI vượt qua 58 test
train/load/inference/evaluate. Evidence chi tiết trong [PLAN.md](../PLAN.md).
