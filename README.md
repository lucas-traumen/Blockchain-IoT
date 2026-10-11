# HRC Safety Log — IoT, AI và Blockchain

Đây là monorepo cho hệ thống an toàn người–robot, gồm ba lớp có ranh giới rõ
ràng:

1. **IoT/AI runtime**: ESP32 gửi telemetry qua MQTT; Python `iot_backend` lưu raw,
   tính ngưỡng an toàn `Sp`, chạy inference và quản lý session/dataset.
2. **Blockchain**: Smart Contract Solidity lưu hash/sự kiện safety; Node.js
   API xử lý canonicalize, ký/gửi giao dịch, chống trùng và đọc lịch sử chain.
3. **Dashboard**: frontend HTML/CSS/JavaScript kết nối API và MetaMask để xem
   trạng thái robot, sự kiện và giao dịch.

ESP32 + HC-SR04 đo khoảng cách 10 Hz; IoT backend lưu raw, tính ngưỡng SSM
`Sp` và dự báo khoảng cách 100–500 ms bằng LSTM. Assessment và
gói sự kiện được lưu SQLite để lớp blockchain/API hoặc TV2 lấy qua API.

## Cấu trúc dự án

Tên thư mục được chọn theo trách nhiệm để tránh nhầm giữa hai backend và giữa
blockchain runtime với smart contract:

```text
Blockchain-IoT/
├── iot_backend/                 # Python: MQTT, ingestion, safety, inference, session
├── ai_model/                    # Windowing, LSTM train/evaluate và baselines
├── firmware/esp32/              # ESP-IDF firmware cho ESP32 + HC-SR04
├── blockchain_api/              # Node.js/Express: hash, auth, transaction, RPC
├── smart_contracts/              # Solidity + Hardhat + deployment/test contract
├── dashboard/                   # HTML/CSS/JS + MetaMask + ethers browser bundle
├── configs/                     # Safety, model, sensor và kế hoạch thu dataset
├── schemas/                     # JSON Schema cho telemetry/control/ack/assessment
├── scripts/                     # Simulator, session, ingest, replay, export, calibration
├── data/                        # SQLite/dataset runtime; không commit dữ liệu thật
├── docs/                        # Wiring, Docker, dataset, LSTM và TV2 handoff
├── tests/                       # Python/firmware tests
├── compose.yaml                 # Mosquitto, IoT backend và AI service
├── Dockerfile                   # Image Python IoT backend
└── README.md
```

### Quy ước tên quan trọng

- `iot_backend` là **Python backend cho IoT/AI**, không phải API blockchain.
- `blockchain_api` là **Node.js backend cho contract**, không nhận telemetry
  MQTT trực tiếp.
- `smart_contracts` chỉ chứa Solidity/Hardhat; không gọi là `blockchain_api`.
- `dashboard` là giao diện người dùng; không đặt tên chung là `frontend` trong
  tài liệu cấp repo để tránh nhầm với các client khác.
- `ai_model` dùng chung cho training và inference preprocessing; không đặt
  TensorFlow hoặc artifact production vào `iot_backend`.

> **Trạng thái migration:** cấu trúc đã được hợp nhất vào repo cha. Các package
> runtime hiện dùng tên `iot_backend`, `blockchain_api`, `smart_contracts` và
> `dashboard`; không tạo lại thư mục `backend/`, `blockchain/` hoặc `frontend/`.

## Phạm vi hệ thống

- IoT/AI: `CLEAR`, `VIOLATION`, `UNKNOWN`; AI chỉ cảnh báo sớm khi trạng thái
  hiện tại còn `CLEAR`.
- Blockchain: payload canonicalize/hash ngoài chain; hash và metadata sự kiện
  được ghi lên contract.
- Dashboard: đọc trạng thái contract và cho owner thao tác Lock/Unlock qua
  MetaMask.
- Dữ liệu synthetic luôn phải được đánh dấu riêng, không coi là phép đo thật.

Đơn vị nội bộ SI (m, s, m/s), telemetry dùng mm/µs. Khóa mẫu là
`(device_id, boot_id, sequence)`. Safety giữ `CLEAR`, `VIOLATION`, `UNKNOWN`;
AI chỉ cảnh báo trước vi phạm khi hiện tại còn CLEAR. Dữ liệu synthetic phải
được phân biệt với phép đo thật.

## Kiến trúc

```text
ESP32 / simulator → MQTT QoS1 → validate → raw SQLite → safety + inference
                                                        ├─ processed_samples
                                                        └─ event_packages → API TV2
ai_model: session split → windows/features → train-only scaler → train/evaluate
```

- Topic: `hrc/v1/devices/{device_id}/telemetry|control|ack`, QoS1, retain=false.
- DB: `HRC_DB_PATH` (mặc định `data/hrc.db`), SQLite WAL, DDL tại
  `iot_backend/ingestion/schema.sql`.
- Session khóa bằng boot/sequence từ ack; khi thiếu ack dùng DB fallback.
- LSTM dùng `(N,10,2)` `[distance_m, delta_distance_m]`, `delta[0]=0`, output
  `(N,5)`. Runtime chỉ nạp artifact LSTM và không tự chuyển model khi artifact lỗi.

## Triển khai trên máy mới: pull image, Compose và dashboard

Đây là luồng chính khi chạy hệ thống trên **một máy khác** đã cài Docker. Máy
đích không cần Node.js, Python hoặc TensorFlow: image AI đã chứa runtime Python,
TensorFlow/Keras và artifact canonical `lstm-v1`; Compose kéo thêm Mosquitto và
Nginx để phục vụ dashboard.

Luồng này cần checkout repo vì `compose.yaml`, `compose.env`, `configs/`,
`docker/mosquitto/` và frontend `dashboard/` được mount từ repo. Chỉ
`docker pull` image mà không có các file Compose/frontend là chưa đủ.

### 1. Chuẩn bị máy đích

Cài Docker Engine và Compose v2, sau đó clone repo:

```bash
git clone https://github.com/lucas-traumen/Blockchain-IoT.git
cd Blockchain-IoT
docker --version
docker compose version
```

`blockchain-hrc:ai` là tag local mặc định. Để pull trên máy khác, trước đó
phải có một tag đã được push vào registry. Ví dụ dưới đây dùng GHCR nhưng có
thể thay bằng Docker Hub hoặc registry nội bộ:

```bash
export HRC_IMAGE=ghcr.io/<org-or-user>/blockchain-hrc:ai
docker login ghcr.io
docker pull "$HRC_IMAGE"
```

Nếu registry private, máy đích phải được cấp quyền đọc package. Không dùng
`docker compose up --build` ở máy đích nếu mục tiêu là chạy đúng image đã phát
hành; lệnh đó sẽ build lại từ source local.

> **Lưu ý image:** image phải được build từ phiên bản có `.dockerignore` mới,
> trong đó artifact `ai_model/artifacts/lstm-v1/` được đóng gói vào image.
> Nếu image cũ không có artifact, `/models/active` sẽ báo
> `model_available=false` dù container vẫn healthy.

Máy phát hành image có thể build và push như sau (cần quyền ghi registry):

```bash
export HRC_IMAGE=ghcr.io/<org-or-user>/blockchain-hrc:ai
docker build --target ai -t "$HRC_IMAGE" .
docker push "$HRC_IMAGE"
```

Sau đó máy đích chỉ dùng `docker pull "$HRC_IMAGE"` và `docker compose ...
--no-build`; không cần copy model artifact riêng.

### 2. Tạo cấu hình Compose

```bash
cp compose.env.example compose.env
${EDITOR:-vi} compose.env
```

Đổi các giá trị theo máy đích:

```dotenv
HRC_IMAGE=ghcr.io/<org-or-user>/blockchain-hrc:ai
HRC_SOURCE_MODE=esp32
HRC_DEVICE_ID=esp32_01
HRC_MQTT_HOST_PORT=1885
HRC_API_HOST_PORT=8001
HRC_DASHBOARD_HOST_PORT=8080
HRC_CORS_ORIGINS=http://<IP-MAY-DOCKER>:8080,http://localhost:8080,http://127.0.0.1:8080
```

Mở firewall cho TCP `1885` (ESP32 → MQTT), `8001` (dashboard → IoT API) và
`8080` (trình duyệt → dashboard). Không mở các cổng này ra Internet nếu chỉ
chạy trong LAN.

### 3. Pull và khởi động toàn bộ runtime IoT

```bash
docker compose --env-file compose.env pull mqtt iot_backend dashboard
docker compose --env-file compose.env up -d --no-build --wait mqtt iot_backend dashboard
docker compose --env-file compose.env ps
```

Các service sau khi khởi động:

| Service | Chức năng | Cổng máy đích |
|---|---|---|
| `mqtt` | Mosquitto nhận telemetry từ ESP32 | `1885` |
| `iot_backend` | Validate, SQLite, safety, LSTM, SSE | `8001` |
| `dashboard` | Nginx phục vụ frontend | `8080` |

Kiểm tra trước khi mở trình duyệt:

```bash
curl -fsS http://127.0.0.1:8001/health
curl -fsS http://127.0.0.1:8001/models/active
curl -fsS http://127.0.0.1:8001/system/health
```

Mở dashboard tại `http://<IP-MAY-DOCKER>:8080/`. Vào mục **Live Monitoring**;
badge phải chuyển sang `LIVE` sau khi backend nhận telemetry. Dashboard đọc
realtime bằng `/live/stream` và tự fallback sang polling nếu SSE bị gián đoạn.

### 4. Cho ESP32 gửi dữ liệu vào máy đích

Trong `firmware/esp32/main/network_config.local.h`, đặt broker là IP LAN của
máy chạy Docker, không dùng `localhost`:

```c
#define HRC_MQTT_URI "mqtt://<IP-MAY-DOCKER>:1885"
```

Build/flash firmware rồi kiểm tra serial log có Wi-Fi và MQTT connected. Xác
nhận dữ liệu đã đi hết luồng bằng:

```bash
docker compose --env-file compose.env logs -f iot_backend
curl -fsS http://127.0.0.1:8001/live/latest
docker compose --env-file compose.env exec mqtt \
  mosquitto_sub -h 127.0.0.1 -t 'hrc/v1/devices/+/telemetry' -C 1 -W 15
```

Kết quả cần thấy là `device_id`, `boot_id`, `sequence`, `distance_mm` và
`received_at` mới. Khi đó dashboard đang hiển thị dữ liệu thật từ ESP32, không
phải dữ liệu simulator. Giữ `HRC_SOURCE_MODE=esp32` để provenance trên API và
dashboard không bị gắn nhãn synthetic.

### 5. Blockchain và Session Rewards trên máy mới

Compose pull ở trên chạy **MQTT + IoT/AI + dashboard**. Image này chưa chứa
Hardhat node hoặc `blockchain_api`, nên dashboard live telemetry hoạt động độc
lập nhưng các mục Blockchain/Session Rewards cần thêm:

1. RPC/Hardhat hoặc một testnet RPC đang chạy;
2. `blockchain_api` chạy ở cổng `3000` với deployment contract và `.env` riêng;
3. `HRC_CORS_ORIGINS` của API cho phép origin `http://<IP-MAY-DOCKER>:8080`;
4. các biến `HRC_REWARD_*`/`HRC_CHAIN_*` trong `compose.env` nếu muốn IoT tự
   settlement hoặc gửi safety event lên chain.

Nếu chưa triển khai blockchain API, bỏ qua các tab Blockchain/Rewards; không
được coi việc dashboard mở được là blockchain đã kết nối.

### 6. Dừng và cập nhật image

```bash
docker compose --env-file compose.env down
docker pull "$HRC_IMAGE"
docker compose --env-file compose.env up -d --no-build --wait mqtt iot_backend dashboard
```

Named volume `blockchain-hrc_backend_data` giữ SQLite khi recreate container.
Không dùng `down -v` nếu cần giữ dữ liệu đã nhận.

## Triển khai và chạy local

Đây là luồng chạy đầy đủ đã được kiểm tra cho demo local. Luồng này dùng
Hardhat `chainId=31337`, Mosquitto, `iot_backend` với LSTM canonical và
`blockchain_api`. Không cần ESP32 để chạy smoke test; simulator MQTT được dùng
thay cho thiết bị thật.

### 0. Điều kiện máy

Cần có Docker Engine/Compose v2, Node.js/npm, Python 3.12 nếu muốn chạy test
hoặc công cụ Python trực tiếp, và `openssl` để tạo bearer token local.

```bash
docker --version
docker compose version
node --version
npm --version
python3 --version
```

Cổng local mặc định:

| Thành phần | Địa chỉ |
|---|---|
| Hardhat RPC | `http://127.0.0.1:8545` |
| Blockchain API | `http://127.0.0.1:3000` |
| Dashboard | `http://127.0.0.1:8080` |
| MQTT từ host/ESP32 | `mqtt://<IP-LAN>:1885` |
| IoT API | `http://127.0.0.1:8001` |

Nếu một cổng đang bị chiếm, sửa `compose.env` hoặc biến `PORT` trước khi chạy.
ESP32 phải dùng IP LAN của máy chạy Docker; `localhost` trên ESP32 không trỏ
về máy host.

### 1. Cài dependency

Chạy từ thư mục repo:

```bash
npm --prefix smart_contracts ci
npm --prefix blockchain_api ci

python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Không cài TensorFlow vào `.venv` baseline. Image Compose target `ai` đã chứa
TensorFlow `2.20.0`, Keras `3.15.1` và được dùng cho runtime LSTM.

### 2. Khởi động và deploy Hardhat local

Mở **terminal 1** và giữ terminal này chạy:

```bash
cd smart_contracts
npx hardhat node
```

Hardhat in ra account và private key local. Ghi lại account đầu tiên và private
key tương ứng; account này dùng làm signer của `blockchain_api` và beneficiary
trong demo. Không dùng private key local trên testnet hoặc mainnet.

Mở **terminal 2**:

```bash
cd smart_contracts
npx hardhat compile
npx hardhat run scripts/deploy.js --network localhost
npx hardhat run scripts/deploy_reward.js --network localhost
```

`deploy.js` ghi `deployments/localhost.json`; `deploy_reward.js` ghi
`deployments/reward-localhost.json`. Nếu restart Hardhat, chain và địa chỉ
contract được tạo lại; phải deploy lại rồi restart blockchain API.

### 3. Cấu hình và chạy blockchain API

Tạo file local, không commit:

```bash
cp blockchain_api/.env.example blockchain_api/.env
${EDITOR:-vi} blockchain_api/.env
```

Đặt các giá trị chính sau trong `blockchain_api/.env`:

```dotenv
RPC_URL=http://127.0.0.1:8545
NETWORK=localhost
PRIVATE_KEY=<private-key-account-dau-tien-cua-hardhat>
PORT=3000

# Để trống khi dùng deployment JSON local.
CONTRACT_ADDRESS=
HRC_REWARD_ADDRESS=
HRC_REWARD_DEPLOYMENT=../smart_contracts/deployments/reward-localhost.json

HRC_INGEST_API_TOKEN=<token-ingest-it-nhat-32-ky-tu>
HRC_ADMIN_API_TOKEN=<token-admin-khac-ingest>
HRC_DASHBOARD_BENEFICIARY=<dia-chi-account-dau-tien-cua-hardhat>
HRC_CORS_ORIGINS=http://127.0.0.1:8080,http://localhost:8080
```

Tạo hai token khác nhau, không dùng token ghi trong tài liệu:

```bash
openssl rand -hex 32
openssl rand -hex 32
```

Mở **terminal 3**:

```bash
cd blockchain_api
node server.js
```

Kiểm tra API:

```bash
curl http://127.0.0.1:3000/
curl http://127.0.0.1:3000/api/config
curl http://127.0.0.1:3000/api/rewards/config
```

### 4. Cấu hình và chạy MQTT + IoT/AI

Tạo file Compose local:

```bash
cp compose.env.example compose.env
${EDITOR:-vi} compose.env
```

Để bật full bridge IoT → reward → blockchain local, đặt tối thiểu:

```dotenv
HRC_RUNTIME_TARGET=ai
HRC_MODEL_TYPE=lstm
HRC_MODEL_SUBDIR=lstm-v1
HRC_SOURCE_MODE=simulator
HRC_DEVICE_ID=esp32_01

HRC_REWARD_API_URL=http://host.docker.internal:3000
HRC_REWARD_INGEST_TOKEN=<trung-voi-HRC_INGEST_API_TOKEN>
HRC_REWARD_BENEFICIARY=<dia-chi-account-hardhat>
HRC_REWARD_AUTO_START=true
HRC_REWARD_AUTO_SETTLE=true

HRC_CHAIN_API_URL=http://host.docker.internal:3000
HRC_CHAIN_INGEST_TOKEN=<trung-voi-HRC_INGEST_API_TOKEN>
HRC_CHAIN_AUTO_SUBMIT=true
```

Build target AI và khởi động service ở **terminal 4**:

```bash
docker compose --env-file compose.env build iot_backend
docker compose --env-file compose.env up -d --force-recreate --wait mqtt iot_backend dashboard
docker compose --env-file compose.env ps
```

Cold start TensorFlow có thể mất một phút. Xác minh runtime:

```bash
curl http://127.0.0.1:8001/health
curl http://127.0.0.1:8001/models/active
curl http://127.0.0.1:8001/system/health
```

Trạng thái mong đợi là `/health` có `status=ok`, `mqtt=true`,
`model_available=true`; `/models/active` có `status=ACTIVE`,
`model_version=lstm-v1`, `training_data=synthetic-only`,
`simulation_mode=true` và `threshold_verified_on_hardware=false`.

Nếu chỉ muốn chạy IoT/AI không nối blockchain, để các biến `HRC_REWARD_*` và
`HRC_CHAIN_*` trống hoặc đặt `HRC_REWARD_AUTO_START=false` và
`HRC_REWARD_AUTO_SETTLE=false`.

### 5. Chạy dashboard khi phát triển local

Khi đã chạy service `dashboard` trong Compose, mở:

```text
http://127.0.0.1:8080/#rewards
```

Nếu chỉ muốn phục vụ frontend bằng Python thay vì container Nginx, dừng service
`dashboard` trước rồi mở terminal khác:

```bash
docker compose --env-file compose.env stop dashboard
python3 -m http.server 8080 --bind 127.0.0.1 --directory dashboard
```

Mở `http://127.0.0.1:8080/#rewards`. Dashboard không chứa private key hoặc
bearer token; thao tác owner Lock/Unlock dùng MetaMask trên Hardhat Local.

### 6. Chạy session simulator end-to-end

Đổi `demo-local-001` thành ID mới ở mỗi lần chạy. Session đã settle chỉ được
retry idempotent với cùng evidence.

Mở session và stake 2 HRC:

```bash
curl -X POST http://127.0.0.1:8001/sessions/start \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-local-001","scenario":"approach","object":"synthetic-target"}'
```

Phát 200 mẫu synthetic qua MQTT:

```bash
docker compose --env-file compose.env run --rm --no-deps iot_backend \
  python -m scripts.device_sim \
  --host mqtt --port 1883 --device-id esp32_01 \
  --samples 200 --scenario approach --record-jsonl /app/data/demo-local-001.jsonl
```

Đóng session và đọc kết quả reward:

```bash
curl -X POST http://127.0.0.1:8001/sessions/demo-local-001/stop
curl http://127.0.0.1:8001/sessions/demo-local-001
curl http://127.0.0.1:8001/sessions/demo-local-001/outcome
curl 'http://127.0.0.1:3000/api/rewards/summary?beneficiary=<dia-chi-account-hardhat>'
```

Khi `HRC_REWARD_AUTO_START=true` và `HRC_REWARD_AUTO_SETTLE=true`, backend sẽ
tự gọi blockchain API lúc START/STOP. Nếu chain hoặc API tạm thời lỗi, safety
và session local vẫn được lưu; retry settlement sau khi dịch vụ trở lại:

```bash
curl -X POST http://127.0.0.1:8001/sessions/demo-local-001/settle
```

### 7. Kiểm tra và xem log

```bash
docker compose --env-file compose.env logs --tail=100 mqtt iot_backend
docker compose --env-file compose.env ps

.venv/bin/python -m pytest -q
npm --prefix blockchain_api test
npm --prefix smart_contracts test
docker compose --env-file compose.env config
```

Release local đã kiểm tra: Python `444 passed, 12 skipped`, blockchain API
`53 passed`, smart contracts `64 passing`. Đây là software/local evidence,
không thay thế hardware validation.

### 8. Dừng, restart và reset

Restart service nhưng giữ dữ liệu:

```bash
docker compose --env-file compose.env restart mqtt iot_backend
docker compose --env-file compose.env up -d --wait mqtt iot_backend
```

Dừng stack nhưng giữ named volume:

```bash
docker compose --env-file compose.env down
```

Xóa toàn bộ SQLite và dữ liệu broker chỉ khi muốn reset local:

```bash
docker compose --env-file compose.env down -v
```

`Ctrl+C` ở terminal Hardhat sẽ reset chain local. Sau đó phải deploy lại
contract, kiểm tra deployment JSON và restart `blockchain_api` trước khi bật
bridge IoT.

### 9. Chạy với ESP32 thật

Simulator chỉ kiểm tra pipeline software. Để chạy ESP32 + HC-SR04:

1. cấu hình Wi-Fi và broker trong `firmware/esp32/main/network_config.local.h`;
2. đặt broker thành `mqtt://<IP-LAN-MAY-CHAY-DOCKER>:1885`;
3. chọn đúng target ESP32/ESP32-S3 rồi build/flash;
4. kiểm tra serial, telemetry, ACK START/STOP và reconnect trước khi thu dữ liệu.

Chi tiết xem [docs/wiring.md](docs/wiring.md),
[firmware/esp32/README.md](firmware/esp32/README.md) và
[docs/data-collection.md](docs/data-collection.md). Runtime vẫn hiển thị
`threshold_verified_on_hardware=false` cho tới khi có bằng chứng đo thật.

### 10. Testnet/public chain

Hardhat local không phải Sepolia, Amoy hoặc production. Muốn triển khai testnet
cần RPC, funded signer, private key riêng, deployment manifest riêng và số block
confirmation phù hợp. Xem [docs/blockchain/contracts.md](docs/blockchain/contracts.md)
và [docs/blockchain/integration.md](docs/blockchain/integration.md); không dùng
private key hoặc địa chỉ contract local cho mạng công khai.

## Blockchain API và dashboard

Demo hiện đã có cầu nối session giữa IoT và blockchain. Khi bật
`HRC_REWARD_AUTO_START=true` và `HRC_REWARD_AUTO_SETTLE=true`, iot_backend sẽ:

1. gọi `POST /api/session-start` để giữ 2 HRC trước session;
2. tính outcome từ raw/processed samples khi session đóng;
3. tạo `evidenceHash` ổn định và gọi `POST /api/session-outcome`;
4. lưu trạng thái `PENDING`/`ERROR`/`SETTLED` trong SQLite để retry không đổi
   bằng chứng hoặc thanh toán trùng.

Blockchain API là thành phần duy nhất giữ private key và ký giao dịch. Dashboard
đọc read-only từ API, hiển thị balance, reward pool, outcome, evidence hash,
transaction hash và block trên Hardhat Local chain `31337`; không đưa private key
hay bearer token vào trình duyệt. Đây là deployment local phục vụ demo, không phải
Sepolia hoặc production.

Chạy kiểm thử từ repo root:

```bash
npm --prefix blockchain_api ci
npm --prefix blockchain_api test
npm --prefix smart_contracts ci
npm --prefix smart_contracts test
```

Compile ABI trước khi khởi động API:

```bash
(cd smart_contracts && npx hardhat compile)
(cd blockchain_api && node server.js)
python3 -m http.server 8080 --bind 127.0.0.1 --directory dashboard
```

Với deployment local hiện tại, Hardhat node và contract phải đang chạy trước API.
Mở dashboard tại `http://127.0.0.1:8080/#rewards`. Các endpoint read-only chính là
`/api/rewards/config`, `/api/rewards/summary`, `/api/rewards/sessions` và
`/api/rewards/transactions`; hai endpoint ghi session yêu cầu ingest bearer token.

API cần RPC, khóa ký và token được cấu hình trong môi trường process hoặc
`blockchain_api/.env`. Xem [cấu hình API](docs/blockchain/integration.md) và
[hướng dẫn contract](docs/blockchain/contracts.md). API blockchain dùng cổng
3000; dashboard dùng 8080; IoT API Docker dùng 8001.

## Chạy bằng Docker

MQTT, iot_backend/API và công cụ AI đã có trong [compose.yaml](compose.yaml).
Chạy từ repo root:

```bash
docker compose up -d --build --wait mqtt iot_backend
docker compose ps
curl http://localhost:8001/health
```

Broker dùng cổng host **1885**, API dùng **8001**; bên trong Docker iot_backend
kết nối `mqtt:1883`. SQLite và dữ liệu broker nằm trên named volume, được giữ
khi restart/recreate container. Runtime báo cáo mặc định dùng LSTM; chưa có artifact thì
`model_available=false`, safety vẫn hoạt động. Profile `ai` cung cấp
TensorFlow để train/evaluate hoặc chạy iot_backend LSTM.

Wi-Fi được khai báo bằng macro `HRC_WIFI_NETWORKS` trong
`firmware/esp32/main/network_config.local.h`: mỗi dòng là `{SSID, mật khẩu}`.
Firmware tạo mảng hai chiều, quét các AP, chọn mạng đã khai báo có RSSI mạnh
nhất và thử mạng tiếp theo nếu nối thất bại. Mất mạng thì quét lại; cấu hình
macro được ưu tiên hoàn toàn trước NVS. File mẫu:
[network_config.example.h](firmware/esp32/main/network_config.example.h).
Local header không commit; sửa rồi build/flash lại. Broker cũng đặt bằng macro
`HRC_MQTT_URI`, cổng Docker **1885**. Xem [docs/docker.md](docs/docker.md) và
[firmware/esp32/README.md](firmware/esp32/README.md) cho cấu hình/build/flash.

## Hướng dẫn thu dataset thật

Luồng tối thiểu cho một lần thu là: **ESP32 → MQTT → iot_backend Docker → mở
session → thực hiện kịch bản → đóng session → export JSONL**. Backend phải được
khởi động trước ESP32; session phải được mở trước khi thực hiện kịch bản để
ranh giới dữ liệu được chốt theo `device_ack` hoặc `db_fallback`.

### 1. Kiểm tra phần cứng và firmware

Theo cấu hình nguyên mẫu hiện tại, HC-SR04 dùng VCC 3,3 V, GND chung, TRIG
GPIO4 và ECHO GPIO5. Chỉ nối Echo trực tiếp sau khi đã kiểm tra mức cao của
đúng module tương thích với GPIO ESP32. Ghi lại mã module, board, nguồn cấp và
kết quả kiểm tra trước khi thu dataset chính thức. Xem
[docs/wiring.md](docs/wiring.md) và [configs/sensor.yaml](configs/sensor.yaml).

Cấu hình Wi-Fi và broker trong file local của firmware:

```c
#define HRC_WIFI_LIST_MODE 1
#define HRC_WIFI_NETWORKS { \
    {"WiFi_cua_ban", "mat_khau_wifi"}, \
}
#define HRC_MQTT_URI "mqtt://<IP_MAY_CHAY_DOCKER>:1885"
```

`localhost` trên ESP32 không trỏ tới máy chạy Docker. Build, flash và kiểm tra
log firmware trước khi thu.

### 2. Khởi động Docker

```bash
docker compose up -d --build --wait mqtt iot_backend
docker compose ps
curl http://localhost:8001/health
```

Phải thấy `status: ok`. `mqtt: true` chỉ cho biết listener đã bật; cần kiểm tra
telemetry thực tế trong log hoặc database để xác nhận ESP32 đã gửi được dữ liệu.

```bash
docker compose logs -f --tail=100 mqtt iot_backend
```

Broker được mở ở cổng host `1885`, API ở `8001`. Nếu thay đổi IP, cổng hoặc
`HRC_DEVICE_ID`, tạo `compose.env` từ [compose.env.example](compose.env.example)
và chạy các lệnh Compose với `--env-file compose.env`.

### 3. Mở và đóng session

Để gắn nhãn thuận tiện cho dữ liệu thật, runner gửi tín hiệu START/STOP xuống
ESP32 qua iot_backend và tự lưu metadata từng lượt:

```bash
.venv/bin/python -m scripts.collect_dataset \
  --output-dir data/datasets/real-20261008
```

Runner hỏi scenario, target type, tốc độ và ghi chú; mỗi lượt nhấn Enter lần
đầu để gửi START, thực hiện đúng một kịch bản, rồi nhấn Enter lần hai để gửi
STOP. Dữ liệu được lưu theo session trong `dataset.jsonl` và `manifest.json`.
Bỏ `--output-dir` để tự tạo thư mục mới mỗi lần chạy; runner giữ thư mục cũ và
báo lỗi nếu thư mục chỉ định đã có dữ liệu.
Có thể chạy kế hoạch lặp tại [configs/collection.example.yaml](configs/collection.example.yaml):

```bash
.venv/bin/python -m scripts.collect_dataset \
  --plan configs/collection.example.yaml \
  --output-dir data/datasets/real-20261008
```

Khi iot_backend chạy bằng Docker, dùng API để iot_backend là tiến trình duy nhất ghi
SQLite:

```bash
# Mở session trước khi thực hiện kịch bản
curl -X POST http://localhost:8001/sessions/start \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"real_approach_slow_001","scenario":"approach","object":"person","zone_note":"0.9m to 0.3m, slow"}'

# Sau khi thực hiện xong kịch bản, đóng session
curl -X POST \
  http://localhost:8001/sessions/real_approach_slow_001/stop

# Kiểm tra session đã đóng và xem thống kê
curl http://localhost:8001/sessions/real_approach_slow_001
```

Mỗi session nên tương ứng với một kịch bản liên tục. Ghi rõ `scenario`, tốc độ
(`slow`, `medium`, `fast`), `target_type` và ghi chú thao tác. Không khởi động
lại ESP32 giữa session; nếu `boot_id` đổi, iot_backend đánh dấu `boot_change` và
không trộn boot mới vào session cũ.

Nên thu tối thiểu các nhóm: đứng yên hai bên ngưỡng `0.04 m` (40 mm), tiến gần với
nhiều tốc độ, rời xa, tiến gần rồi dừng, đổi hướng, dao động quanh ngưỡng và
lỗi cảm biến. Một file hoặc một session đơn lẻ chưa đủ để train/evaluate LSTM;
pipeline chia train/validation/test theo `session_id`. Giai đoạn pilot nên có
50–80 session trước khi chốt pipeline, sau đó mở rộng lên bộ dữ liệu chính.

### 4. Export dataset

Chỉ export sau khi các session đã đóng. Với dữ liệu từ ESP32 thật:

```bash
docker compose exec -T iot_backend python -m scripts.export_dataset \
  --db /app/data/hrc.db \
  --out /app/data/real-sessions-pilot.jsonl \
  --source real

docker compose cp iot_backend:/app/data/real-sessions-pilot.jsonl \
  data/real-sessions-pilot.jsonl
```

Kiểm tra nhanh file export:

```bash
wc -l data/real-sessions-pilot.jsonl
head -n 1 data/real-sessions-pilot.jsonl
```

Không dùng `--source real` cho dữ liệu từ `device_sim.py` hoặc dataset synthetic.
Các dữ liệu đó phải nằm ở DB/dataset riêng và export với `--source synthetic`.
Không chạy `session_cli`, `ingest_jsonl` hoặc replay song song để cùng ghi vào
DB iot_backend Docker. Chi tiết xem [docs/data-collection.md](docs/data-collection.md).

## Train và đánh giá sau khi có đủ session

Chỉ chạy bước này sau khi dataset có đủ session và có các nhóm kịch bản cần đánh giá. Dataset phải giữ metadata session, scenario, tốc độ và target type.

```bash
# Tạo image AI
docker compose --profile ai build ai

# Train LSTM; split theo session và fit scaler chỉ trên train
docker compose run --rm ai python -m ai_model.lstm_train \
  --dataset data/real-sessions-pilot.jsonl \
  --out-dir ai_model/artifacts/lstm-v1

# Đánh giá trên test split với persistence và constant velocity
docker compose run --rm ai python -m ai_model.evaluate \
  --dataset data/real-sessions-pilot.jsonl \
  --model-type lstm \
  --model-dir ai_model/artifacts/lstm-v1 \
  --output data/real-lstm-report.json
```

Nếu dataset chưa đủ session, trainer phải dừng thay vì tạo split giả. Không đưa
artifact train từ synthetic vào runtime dữ liệu thật và không kết luận LSTM tốt
hơn baseline nếu chưa có báo cáo trên test session thật. Xem
[docs/lstm.md](docs/lstm.md) để kiểm tra manifest/hash và bật
`MODEL_TYPE=lstm`.

## Môi trường Python trực tiếp

Python 3.12, chạy từ repo root:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Train/evaluate và runtime iot_backend dùng LSTM cần môi trường riêng:

```bash
python3.12 -m venv .venv-ai
.venv-ai/bin/pip install -r requirements-ai.txt
```

`requirements-ai.txt` pin TensorFlow 2.20.0/Keras 3.15.1 và include các dependency
iot_backend. Không cài TensorFlow vào `.venv` baseline. Xem
[docs/lstm.md](docs/lstm.md) để chọn artifact và runtime.

## Lệnh chính

```bash
# Backend/LSTM runtime; dùng environment AI có TensorFlow
.venv/bin/python -m pytest tests/ -q

# Toàn bộ test, gồm artifact LSTM thật
TF_NUM_INTEROP_THREADS=1 TF_NUM_INTRAOP_THREADS=1 OMP_NUM_THREADS=1 \
  .venv-ai/bin/python -m pytest tests/ -q

# Sinh dữ liệu synthetic cho smoke, giữ nhãn nguồn
.venv/bin/python -m scripts.synthetic_dataset --out data/sim.jsonl \
  --sessions 30 --samples-per-session 200 --seed 42 --scenario all

# Train và đánh giá LSTM offline; test dùng đúng split/hash artifact
.venv-ai/bin/python -m ai_model.lstm_train --dataset data/sim.jsonl \
  --out-dir ai_model/artifacts/lstm-v1
.venv-ai/bin/python -m ai_model.evaluate --model-type lstm \
  --dataset data/sim.jsonl --model-dir ai_model/artifacts/lstm-v1 \
  --output data/lstm-report.json

# Backend LSTM, cần broker tại MQTT_HOST/MQTT_PORT
HRC_SOURCE_MODE=simulator MODEL_TYPE=lstm MODEL_ARTIFACT_DIR=ai_model/artifacts/lstm-v1 \
  .venv-ai/bin/uvicorn iot_backend.main:app --host 127.0.0.1 --port 8000

# MQTT simulator, telemetry synthetic; recording giữ nhãn nguồn
.venv/bin/python -m scripts.device_sim --host localhost --port 1883 \
  --samples 200 --scenario approach --record-jsonl data/simulator.jsonl

# Replay LSTM offline, version mới để giữ nguyên lịch sử live
.venv-ai/bin/python -m scripts.replay --db data/hrc.db --model-type lstm \
  --model-dir ai_model/artifacts/lstm-v1 --processor-version replay-lstm-v1

# Export dataset có session metadata; nguồn phải khai tường minh
.venv/bin/python -m scripts.export_dataset --db data/hrc.db \
  --out data/measured-sessions.jsonl --source real

# Báo cáo hiệu chuẩn từ CSV đo được; không tự sửa configs/safety.yaml
.venv/bin/python -m scripts.calibrate --input data/calibration.csv \
  --output data/calibration-report.json
```

`MODEL_TYPE=lstm` là mặc định cho runtime báo cáo. Khi thiếu TensorFlow/artifact
hoặc model lỗi, prediction trả `MODEL_UNAVAILABLE`; safety vẫn tính. Không tự
chuyển sang model khác âm thầm.

## API

| Method | Route | Dữ liệu |
|---|---|---|
| GET | `/health` | MQTT, DB, processor version, model type/availability |
| POST | `/sessions/start` | Mở session: `scenario`, `object`, `zone_note`, `session_id` |
| POST | `/sessions/{session_id}/stop` | Đóng session |
| GET | `/sessions`, `/sessions/{session_id}` | Session và thống kê |
| GET | `/sessions/{session_id}/export?format=jsonl\|csv` | Raw telemetry |
| GET | `/assessments?session_id=&processor_version=&limit=` | Assessment theo version |
| GET | `/samples/{device_id}/{boot_id}/{sequence}` | Tra assessment |
| GET | `/events?session_id=&processor_version=&limit=` | Trigger + assessment đầu tiên |
| GET | `/events/{event_id}` | Gói raw 2s trước/1s sau và coverage |

`PREDICTED_WARNING` cần 2/3 mẫu cảnh báo, không dùng dự báo stale. `VIOLATION`
và `UNKNOWN` phát sự kiện khi chuyển trạng thái. Gói thiếu raw báo coverage
chưa đủ; không thêm mẫu giả. Xem [docs/tv2_handoff.md](docs/tv2_handoff.md).

## Kiểm chứng và giới hạn

Trạng thái/test cuối cùng ghi trong [PLAN.md](PLAN.md). Phiên 02/10/2026 thực
hiện code, review và test trực tiếp, không dùng subagent. Smoke LSTM là kiểm
tra pipeline trên synthetic; chưa chứng minh chất lượng dự báo trên dữ liệu
HC-SR04 thật.

Firmware đã **build PASS trên ESP-IDF 6.0.1 cho ESP32-S3 và
ESP32** ngày 02/10/2026. Trong phiên đã nhận diện **ESP32-D0WD-V3 revision
v3.1** ở `/dev/ttyUSB0` và **ESP32-S3 revision v0.2** ở `/dev/ttyACM0`.
Cổng có thể đổi; chọn đúng chip/target và cổng khi build/flash. Người dùng đã cung cấp log HRC
chạy thật; bản sửa mới và khả năng đo/mạng trên bo vẫn chưa kiểm chứng.
Điện áp Echo, nhiễu, hiệu chuẩn và soak là các gate phần cứng còn mở.
Tham số robot demo không phải chứng nhận ISO. Xem
[firmware/esp32/README.md](firmware/esp32/README.md).

## Tài liệu

- [docs/docker.md](docs/docker.md): MQTT/iot_backend/AI, cổng và dữ liệu Docker.
- [docs/lstm.md](docs/lstm.md): môi trường, train/evaluate, chọn model, runtime.
- [docs/wiring.md](docs/wiring.md): đấu nối, firmware và broker.
- [docs/data-collection.md](docs/data-collection.md): thu/export dữ liệu theo session.
- [docs/background-dataset-collection.md](docs/background-dataset-collection.md): iot_backend chạy nền, scenario, session và thu nhiều dataset.
- [docs/calibration.md](docs/calibration.md): định dạng và công thức báo cáo hiệu chuẩn.
- [docs/tv2_handoff.md](docs/tv2_handoff.md): assessment và event package.
- [docs/experiments.md](docs/experiments.md): các smoke/thí nghiệm tái lập.
