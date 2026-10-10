# Kế hoạch V2 — AI → luồng vận hành → blockchain → dashboard sản phẩm

> Trạng thái: **PROPOSED — chờ duyệt trước khi sửa source**
>
> Ngày lập: 2026-10-10
>
> Phạm vi: ESP32/HC-SR04, LSTM, iot_backend, blockchain_api, smart contracts,
> reward/session và dashboard.

Tài liệu này là kế hoạch triển khai tiếp theo của HRC Safety Log. Mục tiêu không
phải thêm nhiều chức năng rời rạc, mà biến các khối đang có thành một sản phẩm
demo hoàn chỉnh, có dữ liệu thật, có bằng chứng AI, có đường ghi sự kiện lên
blockchain và có dashboard hiển thị đúng dữ liệu trực tiếp.

Tài liệu này không thay thế lịch sử trong [PLAN.md](../PLAN.md). Khi kế hoạch
được duyệt, từng milestone bên dưới phải được đưa vào PLAN.md với trạng thái,
task key, file được phép sửa, lệnh kiểm tra và evidence tương ứng.

---

## 1. Kết luận điều hành

Hệ thống hiện đã có đủ khối phần mềm cơ bản, nhưng chưa nên gọi là sản phẩm hoàn
chỉnh vì còn bốn khoảng trống quan trọng:

1. Model LSTM thật có MAE/RMSE tương đối tốt nhưng **F1 cảnh báo sớm bằng 0**:
   model chưa phát hiện được crossing trên test thật.
2. Ngưỡng demo 40 mm nằm dưới vùng đo đã khai báo là đã kiểm chứng của HC-SR04
   là 100 mm; cấu hình hiện tự phát cảnh báo về sự không phù hợp này.
3. Sự kiện VIOLATION đã được lưu trong SQLite, nhưng chưa có cầu nối chuẩn từ
   event_packages sang POST /api/safety-event và HRCSafetyLog.
4. Dashboard hiện đọc chủ yếu dữ liệu blockchain lịch sử; biểu đồ khoảng cách,
   dự báo 100–500 ms và trạng thái realtime vẫn là placeholder.

Do đó thứ tự triển khai bắt buộc là:

1. chốt ngưỡng và chất lượng phép đo;
2. sửa dữ liệu và khả năng cảnh báo của AI;
3. khóa luồng runtime và cơ chế xác nhận sự kiện;
4. thêm outbox ghi sự kiện an toàn lên blockchain;
5. cuối cùng mới nối dashboard realtime và hoàn thiện trải nghiệm sản phẩm.

Không được đảo thứ tự bằng cách làm dashboard đẹp trước khi dữ liệu và trạng
thái phía dưới đã có hợp đồng rõ ràng.

---

## 2. Trạng thái hiện tại và khoảng trống

| Khối | Đã có | Khoảng trống phải xử lý |
|---|---|---|
| Phần cứng | ESP32/ESP32-S3 + HC-SR04, đo 100 ms, MQTT QoS1 | Cần hiệu chuẩn vùng gần, soak nhiều session, đo tỷ lệ invalid/outlier |
| Telemetry | telemetry_v1, device/boot/sequence, dedup, SQLite WAL | Payload raw không mang kết quả Edge LSTM; chưa có schema riêng cho edge prediction |
| Safety | CLEAR/VIOLATION/UNKNOWN, Sp, margin | Ngưỡng 40 mm không khớp vùng đo 100 mm; VIOLATION đo tức thời chưa tách với VIOLATION dùng để phạt |
| AI backend | LSTM input 10×2, output 5 khoảng cách 100–500 ms | Test thật: 82 crossing nhưng không có predicted-positive, recall/F1 bằng 0 |
| AI edge | LSTM float32 đã sinh vào firmware, có parity native | Chưa flash/soak đầy đủ; chưa gửi prediction có model hash về backend |
| Event | Debounce warning 2/3, event_packages, raw pre/post | Thiếu log quan sát; thiếu trạng thái confirmation riêng cho vi phạm nhiễu |
| Blockchain safety | HRCSafetyLog và POST /api/safety-event | Chưa tự động lấy event_packages và anchor từng event |
| Blockchain reward | SessionRewards, stake/reward/penalty, durable reward outbox | Cần liên kết rõ với VIOLATION_CONFIRMED, không phạt vì một spike |
| Dashboard | Event, transaction, reward, robot lock/unlock, MetaMask | Realtime chart/forecast chưa hoạt động; URL/contract còn phụ thuộc local/hard-code |
| Vận hành | Docker MQTT + iot_backend; chain/API chạy riêng | Chưa có một lệnh dựng toàn bộ stack và health tổng hợp |

### 2.1 Sự thật phải giữ nguyên trong báo cáo

- Kết quả synthetic tốt chỉ chứng minh pipeline có thể học một quy luật synthetic;
  không thay thế kết quả từ ESP32/HC-SR04 thật.
- MAE thấp không đồng nghĩa hệ thống cảnh báo tốt. Với bài toán này, recall,
  miss rate và lead time của event quan trọng hơn một con số MAE tổng hợp.
- Blockchain không được nằm trong vòng điều khiển an toàn thời gian thực.
- Một giao dịch blockchain thành công không chứng minh cảm biến hoặc AI đúng.
- Đây là demo nghiên cứu, không phải chứng nhận ISO/TS 15066.

---

## 3. Các quyết định kiến trúc bắt buộc

### 3.1 Tách bốn mặt phẳng

| Mặt phẳng | Trách nhiệm | Có được phụ thuộc blockchain không? |
|---|---|---|
| Safety thời gian thực | Đọc cảm biến, tính Sp/margin, phát cảnh báo hoặc stop giả lập | Không |
| AI | Dự báo 5 horizon, tạo predicted warning khi hiện tại còn CLEAR | Không |
| Audit/blockchain | Băm và ghi sự kiện đã xác nhận, settlement session | Có, chạy bất đồng bộ |
| Presentation | Dashboard realtime, lịch sử, transaction, reward | Chỉ đọc; lỗi UI không ảnh hưởng safety |

### 3.2 Nguồn sự thật

- Raw measurement: telemetry_v1 trong raw_samples.
- Trạng thái safety có thẩm quyền: iot_backend tính từ raw và safety config.
- Edge LSTM: dùng cho phản ứng sớm tại thiết bị và đối chiếu parity; không tự
  quyết định token.
- Backend LSTM: bản tái tính có model/scaler/hash đã xác minh, dùng để lưu
  assessment và tạo event chính thức.
- Event đầy đủ: event_packages trong SQLite.
- Bằng chứng bất biến: dataHash và metadata tối thiểu trên HRCSafetyLog.
- Kết quả thưởng/phạt: HRCSessionRewards sau khi session đóng.

### 3.3 Dữ liệu nào lên chain

Không đưa raw telemetry 10 Hz hoặc toàn bộ 5 prediction lên chain. Chain chỉ lưu:

- eventId;
- deviceId;
- timestamp phép đo;
- dataHash của payload canonical đầy đủ;
- riskLevel;
- transaction hash/block từ receipt;
- session outcome/evidence hash ở contract reward riêng.

Payload đầy đủ, raw window và prediction giữ off-chain trong SQLite. Cách này
giảm transaction, tránh lộ dữ liệu thừa và vẫn cho phép chứng minh payload
không bị sửa.

### 3.4 Hai loại quyết định vi phạm

Để vừa an toàn vừa tránh phạt vì nhiễu:

- **VIOLATION_OBSERVED**: một mẫu valid có distance ≤ Sp. Kích hoạt cảnh báo/stop
  cục bộ ngay, không chờ blockchain.
- **VIOLATION_CONFIRMED**: tối thiểu 2 trong 3 mẫu valid liên tiếp vi phạm,
  cùng device/boot/session/config và đúng nhịp. Chỉ trạng thái này mới:
  - tạo event chính thức để anchor lên chain;
  - làm session đủ điều kiện penalty;
  - hiển thị là confirmed trên dashboard.

UNKNOWN hoặc mất cảm biến vẫn đi vào fail-safe nhưng không tự động phạt token.
Nếu nhóm muốn giữ event VIOLATION hiện tại, trường confirmation phải được thêm
vào event package thay vì đổi tên trạng thái safety.

### 3.5 Cấu hình ngưỡng

Phải duy trì hai profile rõ ràng:

| Profile | Mục đích | Ngưỡng |
|---|---|---|
| hrc-ssm-v1 | Demo công thức SSM một chiều | Sp động; fixture hiện tại khoảng 0,44 m |
| square-bench-v2 | Bàn thử hình học | Ngưỡng cố định, nhưng phải nằm trong vùng cảm biến đã hiệu chuẩn |

Ngưỡng 40 mm chỉ được giữ làm benchmark hoặc demo thật nếu phép hiệu chuẩn vùng
20–100 mm đạt toàn bộ gate:

- bias tuyệt đối ≤ 5 mm;
- MAE ≤ 5 mm;
- p95 sai số tuyệt đối ≤ 10 mm;
- invalid rate < 1%;
- không có bước nhảy giả trên 100 mm trong 30 phút stationary;
- kết quả lặp lại với ít nhất ba loại bề mặt mục tiêu.

Nếu không đạt, square-bench-v2 phải nâng ngưỡng lên ít nhất 100–150 mm hoặc dùng
cảm biến phù hợp hơn. Không được chỉ sửa measurement_zone_m để xóa warning.

---

## 4. Kiến trúc đích

~~~mermaid
flowchart TD
    A["ESP32 + HC-SR04<br/>raw safety + Edge LSTM"] --> B["MQTT QoS1<br/>telemetry + edge prediction"]
    B --> C["IoT backend<br/>validate, session, safety, LSTM"]
    C --> D["SQLite WAL<br/>raw, assessment, event, outbox"]
    D --> E["Blockchain API<br/>canonicalize, sign, retry"]
    E --> F["HRCSafetyLog<br/>HRCSessionRewards"]
    C --> G["Live REST/SSE"]
    F --> H["Read-only chain API"]
    G --> I["Dashboard"]
    H --> I
~~~

### 4.1 Luồng nhanh và luồng chậm

~~~mermaid
sequenceDiagram
    participant S as ESP32
    participant M as MQTT
    participant I as IoT backend
    participant B as Blockchain API
    participant C as Contracts
    participant D as Dashboard

    S->>S: Đo + kiểm tra ngưỡng tức thời
    S->>S: Edge LSTM dự báo 100–500 ms
    S->>M: telemetry_v1 + edge_prediction_v1
    M->>I: QoS1 delivery
    I->>I: Commit raw trước
    I->>I: Safety + backend LSTM + detector
    I-->>D: SSE live snapshot
    I->>I: Ghi event + chain outbox
    I->>B: POST safety-event bất đồng bộ
    B->>C: Ghi dataHash/risk
    C-->>B: Receipt
    B-->>I: txHash + block
    I-->>D: Event chuyển CONFIRMED_ON_CHAIN
~~~

Blockchain bị chậm hoặc dừng chỉ làm outbox ở trạng thái chờ; đo, safety, AI,
event local và dashboard realtime vẫn phải tiếp tục.

---

## 5. Hợp đồng dữ liệu đích

### 5.1 Giữ nguyên telemetry_v1

telemetry_v1 tiếp tục chỉ chứa dữ liệu nguồn:

- schema_version;
- device_id;
- boot_id;
- sequence;
- monotonic_us;
- measured_at;
- time_synced;
- distance_mm;
- echo_us;
- valid;
- error_code.

Không nhét model output vào schema này để tránh phá firmware/backend hiện có và
giữ raw measurement độc lập với thuật toán.

### 5.2 Thêm edge_prediction_v1

Topic đề xuất:

~~~text
hrc/v1/devices/{device_id}/edge-prediction
~~~

Payload:

~~~json
{
  "schema_version": "1.0",
  "sample_ref": {
    "device_id": "esp32_01",
    "boot_id": "boot_x",
    "sequence": 14727
  },
  "source": "esp32-edge",
  "model_version": "lstm-v2",
  "model_sha256": "...",
  "scaler_sha256": "...",
  "preprocessing_version": "pp-lstm-v2",
  "threshold_m": 0.15,
  "horizons_ms": [100, 200, 300, 400, 500],
  "distance_m": [0.20, 0.17, 0.14, 0.12, 0.10],
  "predicted_violation_ms": 300,
  "early_warning": true,
  "inference_us": 8200,
  "valid": true
}
~~~

Quy tắc:

- cùng sample_ref với telemetry đã tạo prediction;
- QoS1, retain=false;
- message trễ được lưu để audit nhưng không rewind live detector;
- model/scaler hash phải khớp manifest đã đăng ký;
- payload không thay thế telemetry;
- mất edge prediction không làm mất raw safety.

### 5.3 Backend assessment

assessment tiếp tục có:

- distance hiện tại;
- safety.status, Sp, margin, config version;
- backend prediction 5 horizon;
- stale, model version, predicted_violation_ms;
- session_id.

Nên bổ sung khối provenance trong schema version mới:

~~~json
{
  "prediction_provenance": {
    "authority": "backend",
    "model_sha256": "...",
    "scaler_sha256": "...",
    "edge_available": true,
    "edge_backend_max_abs_diff_m": 0.00008,
    "parity_status": "MATCH"
  }
}
~~~

Không sửa âm thầm assessment_v1 có additionalProperties=false. Chọn một trong
hai cách và ghi migration rõ:

1. tạo assessment_v2; hoặc
2. thêm field optional vào schema v1 và tăng processor_version.

Phương án khuyến nghị: assessment_v2 + processor_version safety-v2 để replay và
so sánh được với lịch sử.

### 5.4 Event package v2

Event chính thức cần thêm:

- observed_ref và confirmed_ref;
- confirmation_rule và evidence_count;
- current safety + AI forecast;
- sensor quality;
- pre/post raw window và coverage;
- model/config/hash provenance;
- payload_hash;
- chain_status, transaction_hash, block_number;
- created_at, submitted_at, confirmed_at;
- retry_count và last_error.

Các trạng thái chain:

~~~text
LOCAL_ONLY
OUTBOX_PENDING
SUBMITTED
CONFIRMED
RETRYABLE_ERROR
DEAD_LETTER
~~~

### 5.5 Ánh xạ risk

| Trạng thái nguồn | riskLevel | Ghi chain? | Tác động reward |
|---|---:|---|---|
| CLEAR | 0 | Không ghi từng mẫu | Có thể dẫn tới VALID_NO_VIOLATION khi session đủ coverage |
| PREDICTED_WARNING đã debounce | 2 | Có | Không cộng/trừ token |
| VIOLATION_OBSERVED chưa xác nhận | 3 cục bộ | Chưa | Chưa phạt |
| VIOLATION_CONFIRMED | 3 | Có | Session có thể nhận PENALTY khi đóng |
| UNKNOWN thoáng qua | 2 | Chỉ lưu local | Không phạt |
| SENSOR_FAULT kéo dài/xác nhận | 2 | Có thể ghi một event | NO_REWARD, hoàn stake |

---

## 6. Kế hoạch xử lý AI

### 6.1 Mục tiêu AI

AI không thay thế công thức safety. Nó phải trả lời câu hỏi:

> Khi khoảng cách hiện tại vẫn lớn hơn Sp, trong 100–500 ms tới có khả năng
> chạm hoặc xuống dưới Sp hay không, và sớm nhất ở horizon nào?

Output tối thiểu vẫn là năm khoảng cách dự báo. Cảnh báo sản phẩm chỉ hợp lệ khi:

- current safety là CLEAR;
- prediction status là OK;
- prediction không stale;
- dữ liệu cửa sổ liên tục và sensor valid;
- warning đạt debounce;
- model/config/hash đúng phiên bản đã duyệt.

### 6.2 Gate AI-0 — sửa bài toán đo trước khi train

Không train lại ngay trên cùng lỗi dữ liệu. Thực hiện:

1. chọn profile ngưỡng;
2. hiệu chuẩn cảm biến ở 20, 30, 40, 50, 75, 100, 150, 200, 300, 440,
   600 và 1000 mm;
3. mỗi điểm tối thiểu 200 mẫu;
4. ba loại bề mặt: phẳng cứng, vải/quần áo, bề mặt nghiêng;
5. lưu khoảng cách tham chiếu độc lập, không chỉ lấy HC-SR04 làm chân lý;
6. xuất bias, MAE, RMSE, p95, invalid rate và outlier rate theo distance/material.

Deliverable:

- data/calibration/{run_id}.csv;
- calibration-report.json;
- đồ thị measured-vs-reference;
- quyết định giữ/bỏ 40 mm có chữ ký/ngày/version.

### 6.3 Kế hoạch dataset thật

Mỗi session chỉ chứa một hành vi liên tục, không reboot giữa session. Metadata
bắt buộc:

- session_id, device_id, boot_id;
- ngày/ca đo và operator;
- scenario;
- speed_level và tốc độ tham chiếu nếu đo được;
- target_type/material/angle;
- safety profile và Sp;
- firmware/model/config hash;
- khoảng cách bắt đầu/kết thúc;
- synthetic=false;
- chất lượng PASS/WARN/REJECT và lý do.

Ma trận tối thiểu:

| Nhóm | Số session tối thiểu | Ghi chú |
|---|---:|---|
| Stationary CLEAR | 20 | Nhiều khoảng cách trong vùng an toàn |
| Stationary gần Sp | 20 | Hai phía của Sp, không rung vật |
| Approach crossing chậm | 20 | Có đoạn CLEAR dài trước crossing |
| Approach crossing vừa | 20 | Độc lập session chậm |
| Approach crossing nhanh | 20 | Đủ mẫu nhưng không vượt khả năng 10 Hz |
| Stop-before-Sp | 30 | Âm tính khó, dừng ở nhiều margin |
| Recede | 20 | Đi ra xa để tránh học một chiều |
| Oscillation quanh Sp | 30 | Kiểm tra false warning/chattering |
| Material/angle variations | 30 | Vải, phẳng, nghiêng; phân tầng rõ |
| Sensor fault | 30 | Dataset riêng, không trộn vào forecast regression |

Tối thiểu 210 session cho forecast và 30 session fault. Nếu nguồn lực hạn chế,
pilot có thể dùng 60 session, nhưng không được dùng pilot để tuyên bố đạt tiêu
chí cuối.

Tập test cuối phải:

- thu ở ngày hoặc ca khác train;
- không được chọn lại sau khi xem metric;
- có ít nhất 50 crossing episode;
- có ít nhất 50 negative session khó;
- có ít nhất 20 fault session để test fail-safe;
- không có session/window trùng train hoặc validation.

### 6.4 Tiền xử lý

Giữ nguyên các nguyên tắc:

- split theo session trước windowing;
- scaler fit train-only;
- không nội suy qua invalid/gap/reboot/session boundary;
- không tạo window qua sequence gap;
- không xóa spike mà không ghi audit;
- raw threshold dùng số đo raw valid; filter cho AI không được làm thay đổi raw
  safety status;
- lưu drop statistics theo nguyên nhân.

Thử nghiệm feature theo thứ tự:

1. E0: distance_m + delta_distance_m như hiện tại;
2. E1: thêm velocity estimate và rolling robust slope;
3. E2: thêm quality features như echo_us hợp lệ và recent invalid count;
4. chỉ giữ feature nếu validation cải thiện và firmware tái tạo được chính xác.

Không đưa measured_at wall-clock hoặc session ID vào model.

### 6.5 Label cảnh báo

Với mỗi window kết thúc tại t:

- regression target: d(t+100)...d(t+500);
- crossing label mỗi horizon:
  current distance > Sp và tồn tại future distance ≤ Sp trước hoặc tại horizon;
- loại window current distance ≤ Sp khỏi positive early-warning vì đó đã là
  VIOLATION;
- UNKNOWN/invalid không gán thành negative;
- event episode được gom theo crossing liên tục, không tính từng window là một
  sự kiện độc lập.

### 6.6 Chiến lược model

#### Bước AI-1 — giữ kiến trúc đơn giản nhưng sửa dữ liệu/loss

Đầu tiên giữ LSTM regression hiện tại để cô lập nguyên nhân:

- input 10×2;
- LSTM 32;
- Dropout 0,2;
- Dense 32 ReLU;
- Dense 5;
- weighted Huber: tăng trọng số cho window gần Sp và window trước crossing;
- sampler cân bằng crossing/negative khó ở mức session/window;
- không oversample test.

Nếu đạt gate event sau debounce thì giữ kiến trúc này.

#### Bước AI-2 — multi-task LSTM nếu AI-1 không đạt

Nếu regression vẫn có MAE tốt nhưng bỏ sót crossing, chuyển sang LSTM hai đầu:

- head A: năm khoảng cách dự báo;
- head B: năm xác suất crossing theo horizon;
- loss = weighted Huber + λ × weighted binary cross-entropy;
- λ và class weight chỉ chọn trên validation;
- threshold xác suất được calibrate trên validation, test chỉ chạy một lần;
- output vẫn map được về predicted_violation_ms.

Model vẫn là LSTM-only; persistence và constant velocity chỉ dùng làm baseline
offline.

### 6.7 Metric bắt buộc

#### Regression

- MAE và RMSE từng horizon;
- bias từng horizon;
- MAE trong vùng near-threshold;
- p50/p95 absolute error;
- breakdown theo speed, scenario, material và ngày thu.

#### Event

- precision, recall, F1;
- miss rate;
- false warnings mỗi negative session và mỗi phút;
- lead time p10/p50/p90;
- số crossing episode support;
- metric trước debounce và sau detector 2/3;
- confusion matrix ở cấp episode.

#### Runtime

- inference latency p50/p95/p99;
- stale rate;
- INVALID_WINDOW rate;
- model availability;
- edge/backend parity;
- heap/stack/flash trên ESP32-S3;
- số mẫu bị trễ hoặc mất do inference.

### 6.8 Gate chấp nhận AI

| Tiêu chí trên test thật độc lập | Gate tối thiểu |
|---|---:|
| Crossing episode support | ≥ 50 |
| Negative session support | ≥ 50 |
| Recall sau debounce | ≥ 0,85 |
| Precision sau debounce | ≥ 0,90 |
| F1 sau debounce | ≥ 0,87 |
| Miss rate | ≤ 0,15 |
| False warning | ≤ 0,10/session âm tính |
| Lead time median | ≥ 200 ms |
| Lead time p10 | ≥ 100 ms |
| MAE 500 ms | ≤ 10 mm hoặc tốt hơn baseline có ý nghĩa |
| Edge inference p95 | < 50 ms |
| Edge/backend max absolute difference | ≤ 0,1 mm cho vector parity cố định |
| Sampling | Không làm mất deadline 100 ms |

Nếu không đạt recall/F1, dashboard phải hiển thị AI là EXPERIMENTAL và sản phẩm
không được quảng bá là cảnh báo sớm đáng tin cậy.

### 6.9 Artifact và promotion

Mỗi artifact phải có:

- model.keras;
- scaler.json;
- manifest.json;
- split_manifest.json;
- metrics.json;
- calibration.json nếu có probability head;
- model/scaler/dataset/split SHA-256;
- source commit;
- TensorFlow/Keras version;
- architecture, features, units, horizons;
- threshold profile được đánh giá;
- acceptance status.

Promotion:

~~~text
candidate → offline_validated → shadow_runtime → hardware_validated → active
~~~

Không ghi đè thư mục artifact active. Dùng thư mục versioned và đổi symlink/env
chỉ sau khi gate pass.

### 6.10 Triển khai Edge AI

1. sinh source weights/scaler từ artifact active;
2. kiểm tra hash trong firmware;
3. parity test với tối thiểu 100 vector gồm near-threshold và outlier;
4. build ESP32-S3;
5. flash, ghi serial model hash và thời gian inference;
6. chạy 30 phút stationary, 30 session động và reconnect test;
7. phát edge_prediction_v1;
8. backend đối chiếu edge/backend và ghi parity_status.

Nếu inference làm trễ chu kỳ đo:

- ưu tiên raw sampling;
- chạy inference task riêng;
- bỏ prediction của cửa sổ cũ thay vì bỏ raw sample;
- không giảm QoS hoặc xóa raw safety để cứu AI.

---

## 7. Luồng runtime chi tiết

### 7.1 Startup

1. ESP32 tạo boot_id mới.
2. Khởi tạo sensor, Wi-Fi, MQTT và model.
3. In model_version, model hash, scaler hash, threshold profile.
4. Backend mở SQLite, chạy migrations và load safety/model.
5. Backend catch-up raw chưa xử lý trước khi nhận stream mới.
6. MQTT subscribe thành công mới đánh dấu control ready.
7. Dashboard chỉ báo ONLINE khi cả live timestamp và backend health còn mới.

### 7.2 Xử lý mỗi mẫu

1. ESP32 bắt đầu đo và chốt monotonic_us/measured_at.
2. Kiểm tra sensor validity.
3. So raw distance với threshold cục bộ để fail-safe.
4. Đẩy sample vào Edge LSTM nếu window hợp lệ.
5. Publish telemetry_v1.
6. Publish edge_prediction_v1 nếu inference hoàn tất đúng sample.
7. Backend validate schema và device/topic.
8. Insert raw idempotent theo device/boot/sequence.
9. Gán session theo boundary ACK.
10. Tính safety.
11. Chạy backend LSTM.
12. Ghép/đối chiếu edge prediction nếu đã nhận.
13. Ghi assessment.
14. Chạy warning debounce và violation confirmer.
15. Ghi event + outbox trong cùng transaction.
16. Phát snapshot cho SSE.
17. Worker outbox gửi blockchain ngoài transaction ingest.

### 7.3 Thứ tự ưu tiên quyết định

~~~text
sensor invalid
    → UNKNOWN / fail-safe / không phạt
sensor valid và distance ≤ Sp
    → VIOLATION_OBSERVED ngay
VIOLATION đạt confirmation
    → VIOLATION_CONFIRMED + chain event + penalty eligibility
current CLEAR + AI OK + not stale + forecast crossing
    → warning candidate
warning đạt 2/3
    → PREDICTED_WARNING + chain event
còn lại
    → CLEAR
~~~

### 7.4 Session

- Dashboard/runner gửi START.
- Device ACK trả boot_id/sequence.
- Samples từ start_sequence được gắn session.
- Nếu blockchain reward enabled, start on-chain là bất đồng bộ; local session
  vẫn phải mở được và trạng thái reward_start hiển thị riêng.
- STOP chốt boundary.
- Backend catch-up processed coverage.
- Policy tính outcome.
- Safety event anchoring và reward settlement là hai outbox độc lập.
- Session không có reward bridge vẫn giữ đầy đủ local evidence.

### 7.5 Failure behavior

| Lỗi | Safety/AI | Lưu dữ liệu | Blockchain | Dashboard |
|---|---|---|---|---|
| Sensor timeout | UNKNOWN, fail-safe | Lưu raw invalid | Không phạt; có thể ghi fault event | Đỏ/UNKNOWN, hiện error_code |
| Mất MQTT ngắn | ESP32 local safety vẫn chạy | Buffer hữu hạn, đếm drop | Không ảnh hưởng | STALE/DISCONNECTED |
| Backend restart | Edge safety vẫn chạy | WAL + catch-up | Outbox tiếp tục retry | Reconnecting |
| Model backend lỗi | Safety trực tiếp vẫn chạy | MODEL_UNAVAILABLE | Không tạo AI warning | AI unavailable |
| Edge model lỗi | Backend model tiếp tục | Lưu edge error/counter | Không ảnh hưởng | Edge unavailable |
| Chain/API lỗi | Safety/event local bình thường | OUTBOX_PENDING/ERROR | Retry có idempotency | Pending, không giả confirmed |
| Dashboard lỗi | Không ảnh hưởng | Không ảnh hưởng | Không ảnh hưởng | Người vận hành reload |

### 7.6 Logging và observability

Phải thêm structured log cho:

- RAW_ACCEPTED/RAW_DUPLICATE/RAW_REJECTED;
- SAFETY_TRANSITION;
- AI_STATUS_CHANGE;
- PREDICTED_WARNING_CONFIRMED;
- VIOLATION_OBSERVED/CONFIRMED;
- EVENT_CREATED;
- CHAIN_SUBMITTED/CONFIRMED/RETRY;
- SESSION_STARTED/STOPPED/OUTCOME;
- REWARD_SETTLED;
- SSE_CLIENT_CONNECTED/DROPPED.

Ví dụ console:

~~~text
SAFETY_TRANSITION device=esp32_01 boot=... seq=14727
from=CLEAR to=VIOLATION distance_m=0.026 sp_m=0.040 margin_m=-0.014
session_id=... model=lstm-v2 stale=false
~~~

Không log private key, bearer token, Wi-Fi password hoặc payload signature.

Metric tối thiểu:

- telemetry_received_total;
- telemetry_duplicate_total;
- telemetry_invalid_total;
- processing_latency_ms;
- inference_latency_ms;
- prediction_stale_total;
- warning/violation/fault_event_total;
- chain_outbox_pending;
- chain_submit_failures_total;
- sse_clients;
- mqtt_reconnect_total.

---

## 8. Kế hoạch blockchain

### 8.1 Giữ hai use case độc lập

| Contract | Khi ghi | Mục đích |
|---|---|---|
| HRCSafetyLog | Mỗi warning/violation/fault đã xác nhận | Chứng minh sự kiện không bị sửa |
| HRCSessionRewards | START và OUTCOME của session | Stake, reward, penalty |

Không dùng transaction reward thay cho safety event, và không phát token theo
từng telemetry sample.

### 8.2 Safety event bridge

Thêm worker trong iot_backend:

~~~text
event_packages
    → canonical event payload
    → chain_event_outbox
    → POST /api/safety-event
    → blockchain_api durable transaction store
    → HRCSafetyLog
    → receipt
    → update outbox/event chain state
~~~

Worker không chạy trong MQTT callback. Nó phải:

- claim row theo transaction SQLite;
- giới hạn một request đang xử lý trên cùng signer;
- exponential backoff có jitter;
- giữ nguyên eventId và payload trên mọi retry;
- phân biệt lỗi retryable và permanent;
- không tạo payload mới khi đã có tx pending;
- reconcile bằng cùng POST khi nhận 202;
- ghi dead-letter sau số lần cấu hình, không xóa row.

### 8.3 Bảng chain_event_outbox

Đề xuất:

~~~sql
CREATE TABLE chain_event_outbox (
  event_id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  transaction_hash TEXT,
  block_number INTEGER,
  last_http_status INTEGER,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  confirmed_at TEXT
);
~~~

Không sửa payload_json/payload_hash sau lần PREPARED đầu tiên.

### 8.4 Ánh xạ event_package sang safety-event

| blockchain_api field | Nguồn |
|---|---|
| schemaVersion | cố định 1.0 |
| eventId | event_packages.event_id |
| deviceId | trigger_ref.device_id |
| bootId | trigger_ref.boot_id |
| sessionId | event.session_id |
| sequence | confirmed_ref.sequence |
| timestamp | measured_at đổi sang Unix seconds |
| sensorData.distance | assessment.distance_m |
| sensorData.valid | assessment.safety.sensor_valid |
| sensorData.threshold | assessment.safety.sp_m |
| sensorData.margin | assessment.safety.margin_m |
| aiResult.model | model_version |
| aiResult.state | PREDICTED_WARNING, VIOLATION hoặc UNKNOWN |
| aiResult.riskLevel | theo bảng risk |
| aiResult.earlyWarning | prediction.early_warning |
| aiResult.predictedViolationMs | predicted_violation_ms |

Payload canonical đầy đủ được băm ở blockchain_api. Backend lưu lại hash được
API trả và kiểm tra nó ổn định khi retry.

### 8.5 Reward/session

Giữ policy hiện tại:

- stake: 2 HRC;
- reward hợp lệ đầu tiên trong ngày: 3 HRC;
- daily limit: không phát thêm;
- VIOLATION_CONFIRMED: mất stake 2 HRC;
- SENSOR_FAULT/INCOMPLETE: hoàn stake, không thưởng/phạt.

Điều chỉnh quan trọng:

- violation_count phục vụ policy phải đếm event confirmed, không chỉ một
  safety sample dưới ngưỡng;
- warning không trừ token;
- UNKNOWN không trừ token;
- lỗi chain không đổi outcome local;
- cùng sessionId/evidenceHash phải idempotent;
- session chưa start on-chain phải được retry start trước settle, như bridge
  hiện tại, nhưng mọi trạng thái phải hiển thị trên dashboard.

### 8.6 Contract strategy

Cho đồ án/demo:

- không dùng proxy upgradeable;
- deploy contract version mới khi policy thay đổi;
- deployment JSON chứa chainId, address, ABI version và deployment block;
- dashboard/API đọc deployment JSON hoặc config endpoint, không hard-code;
- local Hardhat là môi trường mặc định;
- Sepolia là gate tùy chọn sau khi local E2E pass.

### 8.7 Security

- PRIVATE_KEY chỉ ở blockchain_api;
- ingest/admin token tối thiểu 32 ký tự và khác nhau;
- dashboard không chứa bearer token;
- CORS allowlist, không dùng wildcard khi triển khai ngoài localhost;
- rate limit endpoint write;
- body limit giữ 64 KiB;
- validate timestamp, identifiers và finite numbers;
- không log secret;
- signer wallet không dùng đồng thời ở script khác;
- outbox và blockchain transaction store phải backup;
- read-only dashboard dùng RPC/API, write robot dùng MetaMask.

### 8.8 Confirmation và finality

- Hardhat local: 1 confirmation;
- testnet: cấu hình tối thiểu 2–3 confirmation;
- production giả định: cấu hình theo chain, không hard-code;
- UI phân biệt SUBMITTED và CONFIRMED;
- reorg/finality sâu là ngoài MVP nhưng transaction phải được recheck khi mở
  event detail.

### 8.9 Gate blockchain

- cùng event gửi 10 lần chỉ có một on-chain event;
- restart iot_backend/blockchain_api giữa pending vẫn reconcile cùng tx/payload;
- payload khác với cùng eventId trả conflict;
- chain tắt 5 phút không mất event;
- sau chain phục hồi, mọi row pending được confirmed;
- reward session retry không trả/thưởng/phạt hai lần;
- không có secret trong HTML/JS/log/git;
- contract, API và integration test đều pass.

---

## 9. Dashboard sản phẩm

### 9.1 Nguyên tắc

Dashboard phải hiển thị ba nguồn, không trộn:

1. **LIVE** từ iot_backend;
2. **LOCAL AUDIT** từ SQLite/event API;
3. **ON-CHAIN CONFIRMED** từ blockchain_api/contracts.

Mỗi card hoặc row phải có nguồn và timestamp. Không dùng on-chain event cuối
cùng để giả làm khoảng cách realtime.

### 9.2 Kiến trúc truy cập

Khuyến nghị thêm reverse proxy cùng origin:

~~~text
/api/iot/*    → iot_backend:8000
/api/chain/*  → blockchain_api:3000
/stream/*     → iot_backend SSE
/             → dashboard static
~~~

Lợi ích:

- không hard-code localhost khác nhau;
- giảm lỗi CORS;
- deploy được bằng một URL;
- dashboard không biết internal hostname;
- dễ cấu hình HTTPS.

### 9.3 API realtime cần bổ sung

| Method | Route | Mục đích |
|---|---|---|
| GET | /live/latest | Snapshot mới nhất của device |
| GET | /live/history?seconds=60 | Dữ liệu vẽ lại chart khi mở trang |
| GET | /live/stream | SSE cho sample/assessment/event/chain update |
| GET | /models/active | Model version/hash/status |
| GET | /models/metrics | Metric artifact đã duyệt |
| GET | /events/{id}/chain | Trạng thái outbox/tx |
| POST | /events/{id}/chain/retry | Retry có auth cho operator |
| GET | /system/health | MQTT, DB, AI, chain bridge, queue depth |

SSE event types:

~~~text
telemetry
assessment
safety_transition
event_created
chain_status
session_status
reward_status
heartbeat
~~~

SSE phải có event id để client reconnect bằng Last-Event-ID. Nếu mất stream,
dashboard chuyển sang polling snapshot 1 giây/lần và hiển thị DEGRADED.

### 9.4 Các màn hình

#### A. Overview

- device online/offline;
- MQTT/backend/model/chain health;
- current session;
- current distance, Sp và margin;
- safety state;
- predicted violation time;
- event count local/on-chain;
- reward balance/pool;
- last update age.

#### B. Live Monitoring

Biểu đồ cửa sổ 30–60 giây:

- actual distance;
- Sp line;
- shaded violation zone;
- five future prediction points từ sample mới nhất;
- PREDICTED_WARNING marker;
- VIOLATION observed/confirmed marker;
- invalid sample gap;
- zoom/pause/reset.

Card cạnh chart:

- sequence/boot/session;
- sensor validity/error;
- model version/hash;
- inference latency;
- prediction age/stale;
- edge/backend parity;
- earliest predicted horizon.

#### C. AI Quality

- active model và promotion status;
- dataset/split hash;
- MAE/RMSE từng horizon;
- precision/recall/F1 sau debounce;
- event support;
- lead-time distribution;
- false warning rate;
- drift/runtime counters;
- nhãn EXPERIMENTAL nếu gate chưa pass.

#### D. Safety Events

Mỗi row:

- eventId;
- event type;
- observed/confirmed time;
- distance/Sp/margin;
- session/device/sequence;
- model/config version;
- local/chain status;
- tx hash/block.

Modal:

- raw 2 giây trước/1 giây sau;
- actual + forecast chart;
- confirmation evidence;
- canonical payload hash;
- transaction receipt;
- nút copy ID/hash;
- retry chỉ cho operator và không chứa token ở browser.

#### E. Sessions

- START/STOP;
- boundary source;
- duration/sample/gap/invalid counts;
- violation confirmed count;
- outcome;
- reward start/settlement;
- evidence hash;
- tx/block;
- export JSONL/CSV.

Session control phải có confirmation UI và trạng thái pending; không gửi lại
POST khi chưa biết request trước thành công hay chưa.

#### F. Blockchain

- network/chainId/latest block;
- contract addresses lấy động từ config API;
- event transaction history;
- submitted/confirmed/error;
- link explorer phù hợp network;
- local Hardhat hiển thị rõ LOCAL, không tạo link giả about:blank.

#### G. Rewards

- beneficiary;
- wallet balance;
- stake/reward/penalty;
- daily reward remaining;
- pool balance;
- session outcome;
- settlement status;
- evidence/transaction.

#### H. Robot/Control

- current lock state;
- event dùng làm lock reference;
- MetaMask account/role/network;
- Lock/Unlock với transaction progress;
- cảnh báo nếu safety vẫn VIOLATION;
- không tự unlock khi UI reload.

#### I. Settings/Diagnostics

Chỉ đọc:

- device/model/safety profile;
- API/RPC network;
- update rates;
- health;
- version/build commit.

Không hiện secret, private key, token hay Wi-Fi password.

### 9.5 Frontend implementation

Giữ vanilla HTML/CSS/JS để giảm migration. Tách app.js thành module:

~~~text
dashboard/
  index.html
  style.css
  config.js
  api.js
  state.js
  live.js
  charts.js
  events.js
  sessions.js
  rewards.js
  blockchain.js
  wallet.js
~~~

- vendor chart library cục bộ hoặc dùng SVG/canvas nội bộ;
- không phụ thuộc CDN khi demo offline;
- contract address/chain lấy từ /api/rewards/config và chain config endpoint;
- textContent cho dữ liệu người dùng, không innerHTML tùy ý;
- abort request cũ khi chuyển view;
- backoff reconnect;
- dữ liệu chart giới hạn/decimate để không tăng RAM vô hạn.

### 9.6 Trạng thái giao diện bắt buộc

Mỗi widget có đủ:

- loading;
- live/connected;
- stale;
- degraded;
- empty;
- permission denied;
- retryable error;
- permanent error.

Không hiển thị ONLINE chỉ vì GET /health trả 200. Device online cần telemetry
mới hơn ngưỡng, ví dụ dưới 2 giây.

### 9.7 Gate dashboard

- chart cập nhật trong dưới 1 giây từ sample ở LAN ổn định;
- không có số liệu demo hard-code;
- reload khôi phục history và SSE;
- mất iot_backend hoặc chain hiển thị hai lỗi độc lập;
- session/event/tx liên kết được bằng ID;
- mobile 360 px và desktop 1440 px dùng được;
- keyboard focus, contrast và aria-live cho trạng thái quan trọng;
- không có secret trong bundle;
- E2E browser test cho CLEAR → WARNING → VIOLATION → CHAIN CONFIRMED.

---

## 10. Thay đổi cơ sở dữ liệu và migration

Không chỉ thêm CREATE TABLE IF NOT EXISTS rồi hy vọng schema đúng. Bổ sung:

~~~text
schema_migrations(version, applied_at, checksum)
~~~

Migration dự kiến:

1. V2 edge_predictions;
2. V3 event confirmation metadata;
3. V4 chain_event_outbox;
4. V5 live snapshot indexes;
5. V6 model registry/runtime metrics.

Bảng edge_predictions:

~~~sql
CREATE TABLE edge_predictions (
  device_id TEXT NOT NULL,
  boot_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  received_at TEXT NOT NULL,
  model_version TEXT NOT NULL,
  model_sha256 TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  PRIMARY KEY (device_id, boot_id, sequence, model_sha256)
);
~~~

Bảng model_registry tối thiểu:

~~~sql
CREATE TABLE model_registry (
  model_sha256 TEXT PRIMARY KEY,
  model_version TEXT NOT NULL,
  scaler_sha256 TEXT NOT NULL,
  preprocessing_version TEXT NOT NULL,
  artifact_path TEXT NOT NULL,
  promotion_status TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  activated_at TEXT
);
~~~

Migration phải:

- backup DB trước;
- chạy idempotent;
- có test từ schema cũ lên mới;
- không xóa event/raw/reward history;
- rollback bằng restore backup, không dùng destructive reset.

---

## 11. Milestone triển khai

### M22 — Chốt safety profile và hiệu chuẩn

Chủ trì: TV3. Phối hợp: TV4.

Phạm vi:

- configs/safety*.yaml;
- configs/sensor.yaml;
- scripts/calibrate.py;
- docs/calibration.md;
- test calibration/safety.

Deliverable:

- report vùng 20–1000 mm;
- quyết định 40 mm;
- profile hrc-ssm-v1 và square-bench-v2;
- dashboard label demo/non-certified.

DoD:

- không còn warning cấu hình bị bỏ qua;
- mọi dataset mới ghi config_version;
- test Sp/margin/profile pass.

### M23 — Dataset thật V2

Chủ trì: TV4. Phối hợp: TV3.

Phạm vi:

- collection plan;
- session runner;
- metadata schema;
- quality audit;
- held-out test manifest.

DoD:

- đạt ma trận session;
- split theo session/ngày;
- test crossing và negative support đủ;
- dataset hash, audit và provenance hoàn chỉnh;
- fault dataset tách riêng.

### M24 — LSTM V2 và metric event

Chủ trì: TV4.

Pha:

1. retrain architecture hiện tại với balanced/weighted loss;
2. đánh giá sau detector thật;
3. chỉ khi fail mới multi-task LSTM;
4. calibration threshold;
5. artifact promotion.

DoD:

- metric regression/event/runtime đầy đủ;
- đạt gate AI hoặc ghi rõ fail;
- không chọn test split theo kết quả;
- baseline offline được báo cáo trung thực;
- artifact candidate không ghi đè active.

### M25 — Edge LSTM, parity và schema prediction

Chủ trì: TV3 + TV4.

Phạm vi:

- edge_prediction_v1 schema;
- firmware inference/log/publish;
- backend ingest/store/parity;
- generator model;
- firmware/native test.

DoD:

- 100 parity vectors pass;
- model/scaler hash khớp;
- inference p95 < 50 ms;
- sampling 100 ms không trễ;
- flash/serial/30-minute soak pass;
- backend hiển thị parity status.

### M26 — Runtime flow, confirmation và observability

Chủ trì: TV3. Phối hợp: TV2.

Phạm vi:

- violation confirmer;
- structured logs;
- metrics;
- event package v2;
- DB migrations;
- live snapshot/SSE.

DoD:

- VIOLATION_OBSERVED phản ứng ngay;
- chỉ VIOLATION_CONFIRMED ảnh hưởng reward;
- warning 2/3 giữ đúng;
- restart/catch-up/idempotency pass;
- console xuất transition/event rõ;
- SSE không chạy trong MQTT callback.

### M27 — Safety event blockchain bridge

Chủ trì: TV2. Phối hợp: TV1.

Phạm vi:

- chain_event_outbox;
- payload mapper;
- worker/retry/reconcile;
- POST /api/safety-event;
- API/event receipt.

DoD:

- warning/violation confirmed xuất hiện trên HRCSafetyLog;
- duplicate/restart/chain outage test pass;
- hash đối chiếu off-chain/on-chain pass;
- no secret leak;
- dashboard đọc được local→pending→confirmed.

### M28 — Reward hardening

Chủ trì: TV1 + TV2.

Phạm vi:

- reward policy dùng confirmed violation;
- start/settle outbox states;
- deployment/config động;
- contract/API test.

DoD:

- reward, daily-limit, penalty, fault/incomplete đúng;
- một spike không gây penalty;
- retry không thanh toán trùng;
- balance/pool/evidence/tx khớp dashboard.

### M29 — Dashboard realtime sản phẩm

Chủ trì: TV2.

Phạm vi:

- reverse proxy;
- live/history/SSE;
- module hóa frontend;
- chart;
- event/session/AI/blockchain/reward pages;
- responsive/accessibility.

DoD:

- không còn placeholder;
- số liệu lấy từ API thật;
- live latency dưới 1 giây;
- state/stale/source rõ ràng;
- contract/network không hard-code;
- browser E2E pass.

### M30 — E2E, hardware acceptance và demo bundle

Chủ trì: cả nhóm.

Kịch bản:

1. CLEAR;
2. approach tạo PREDICTED_WARNING;
3. crossing tạo VIOLATION_OBSERVED rồi CONFIRMED;
4. chain tắt rồi phục hồi;
5. duplicate MQTT;
6. sensor fault;
7. backend restart;
8. session valid reward;
9. session violation penalty;
10. dashboard reconnect.

Deliverable:

- test logs;
- screenshots/video;
- dataset/artifact/deployment hashes;
- transaction receipts;
- final report JSON;
- limitations;
- một lệnh dựng demo.

---

## 12. Phân công theo thành viên

| Thành viên | Trách nhiệm chính | Không được tự ý làm |
|---|---|---|
| TV1 Blockchain | Contracts, deploy, policy on-chain, contract test | Không tự diễn giải raw sensor/AI |
| TV2 Integration/UI | Event mapper, outbox, blockchain API, reverse proxy, dashboard | Không giữ private key ở browser |
| TV3 IoT/Safety | Sensor, firmware, MQTT, session, Sp, confirmer, live API | Không thay model artifact không có manifest |
| TV4 AI/Data | Dataset, preprocessing, LSTM, evaluation, artifact/parity vectors | Không đổi safety status hoặc token policy |

Quy tắc phối hợp:

- TV3 và TV4 cùng chốt schema edge_prediction trước khi code;
- TV2 chỉ map event schema đã duyệt;
- TV1 chỉ nhận outcome/event đã xác nhận;
- dashboard bắt đầu binding sau khi live/event/chain response ổn định;
- mỗi milestone có coder → reviewer/tester theo AGENTS.md;
- không có hai coder sửa cùng worktree cùng lúc.

---

## 13. Ước lượng và đường găng

| Milestone | Công sức ước lượng | Phụ thuộc |
|---|---:|---|
| M22 | 2–4 ngày công | Phần cứng |
| M23 | 6–10 ngày công | M22 |
| M24 | 5–8 ngày công | M23 |
| M25 | 4–6 ngày công | M24 |
| M26 | 4–6 ngày công | M22, contract dữ liệu |
| M27 | 4–6 ngày công | M26 |
| M28 | 2–4 ngày công | M26 |
| M29 | 6–9 ngày công | M26, M27, M28 |
| M30 | 4–7 ngày công | Tất cả |

Với bốn thành viên làm song song hợp lý, thời gian lịch dự kiến khoảng 4–6
tuần. Đường găng là:

~~~text
M22 → M23 → M24 → M25 → M26/M27 → M29 → M30
~~~

Blockchain contract/reward có thể chuẩn bị song song, nhưng không được chốt
penalty trước khi confirmation semantics ở M26 hoàn tất.

---

## 14. Ma trận kiểm thử tổng

| Lớp | Unit | Integration | E2E/Hardware |
|---|---|---|---|
| Sensor | conversion/error code | firmware telemetry | ruler/reference + surfaces |
| Safety | Sp/margin/state | config version/replay | crossing thật |
| AI | window/scaler/loss/metric | artifact/runtime/parity | edge latency + event recall |
| MQTT | queue/dedup/gap | reconnect/catch-up | Wi-Fi outage/soak |
| Event | debounce/confirmation | atomic event+outbox | warning/violation sequence |
| Blockchain API | validation/idempotency | durable send/restart | Hardhat/Sepolia receipt |
| Contracts | policy/access | API-call contract | balance/pool/session |
| Dashboard | renderer/state | REST/SSE/reconnect | browser scenario |

Test bắt buộc trước merge:

~~~text
Python unit/integration
AI environment tests
native firmware harness
ESP-IDF build ESP32-S3
Node blockchain_api tests
Hardhat contract tests
dashboard browser E2E
git diff --check
secret scan
~~~

---

## 15. Kịch bản demo cuối

### 15.1 Khởi động

1. Dựng MQTT, iot_backend, blockchain node, deploy contracts, blockchain_api,
   reverse proxy và dashboard bằng một compose profile.
2. Health tổng hợp phải xanh.
3. Dashboard hiển thị device chưa online cho tới khi có telemetry mới.
4. ESP32 boot và công bố firmware/model/config hash.

### 15.2 Session an toàn

1. Operator START session.
2. Stake 2 HRC chuyển sang started.
3. Vật giữ ngoài Sp.
4. Dashboard vẽ actual/forecast, trạng thái CLEAR.
5. STOP sau đủ duration/coverage.
6. Outcome VALID_NO_VIOLATION.
7. Contract hoàn stake và thưởng theo daily policy.
8. Dashboard hiện evidence hash, tx và balance.

### 15.3 Cảnh báo rồi vi phạm

1. START session mới.
2. Vật tiến gần từ vùng CLEAR.
3. AI tạo warning đủ 2/3 trước crossing.
4. Dashboard chuyển vàng và hiển thị lead time.
5. Vật vượt Sp: local stop/cảnh báo ngay.
6. Sau 2/3 mẫu valid, event VIOLATION được confirmed.
7. Event local chuyển pending rồi on-chain confirmed.
8. STOP session; outcome VIOLATION_CONFIRMED.
9. Contract thu stake/penalty đúng policy.

### 15.4 Sensor fault

1. Rút/che sensor để tạo timeout.
2. Trạng thái UNKNOWN/fail-safe.
3. Không phạt token.
4. Session outcome SENSOR_FAULT.
5. Stake được hoàn.

### 15.5 Chain outage

1. Dừng Hardhat/blockchain_api.
2. Tiếp tục tạo event.
3. Dashboard vẫn live, event ở OUTBOX_PENDING.
4. Khởi động chain/API lại.
5. Worker retry cùng eventId/payload.
6. Event confirmed một lần duy nhất.

---

## 16. Definition of Done toàn sản phẩm

Chỉ được đánh dấu hoàn thành khi đồng thời đạt:

### Hardware/IoT

- sensor calibration pass cho vùng vận hành;
- flash đúng firmware/model active;
- telemetry 10 Hz ổn định;
- reconnect và 30-minute soak pass;
- không mất deadline vì AI.

### AI

- test set độc lập đủ support;
- đạt gate precision/recall/F1/lead time sau debounce;
- artifact/hash/split/scaler audit pass;
- edge/backend parity pass;
- dashboard hiện đúng model status.

### Flow

- raw commit trước compute;
- dedup, session boundary, catch-up, event atomic;
- observed/confirmed semantics đúng;
- lỗi từng khối có fail-safe rõ.

### Blockchain

- safety event tự động on-chain;
- outbox bền vững và idempotent;
- reward/penalty theo confirmed outcome;
- receipt/hash đối soát được;
- không lộ secret.

### Dashboard

- realtime data thật, không placeholder;
- chart actual/Sp/prediction;
- event local/pending/on-chain;
- session/reward/transaction đầy đủ;
- stale/degraded/error rõ;
- responsive và browser E2E pass.

### Evidence

- report machine-readable;
- logs;
- screenshots/video;
- commit/model/dataset/config/contract hash;
- lệnh tái lập;
- danh sách giới hạn.

---

## 17. Điều kiện dừng và rollback

Dừng promotion nếu:

- test split bị leakage;
- 40 mm không qua calibration;
- F1/recall không đạt;
- edge/backend lệch vượt tolerance;
- AI làm trễ sampling;
- event duplicate on-chain;
- reward có thể settle hai lần;
- dashboard chứa key/token;
- chain outage làm dừng safety.

Rollback:

- đổi MODEL_ARTIFACT_DIR về artifact active trước;
- firmware quay về model hash trước;
- giữ safety trực tiếp hoạt động;
- pause chain worker nhưng không xóa outbox;
- không reset DB/contract để che lỗi;
- ghi rõ version và lý do rollback.

---

## 18. Thứ tự phê duyệt

Trước khi triển khai source, nhóm cần duyệt bốn quyết định:

1. Giữ 40 mm sau calibration hay chuyển sang ngưỡng nằm trong vùng ≥100 mm.
2. Chấp nhận mô hình hai tầng: edge dự báo nhanh, backend là authority/audit.
3. Chỉ dùng VIOLATION_CONFIRMED cho penalty.
4. Hoàn tất local Hardhat trước; Sepolia chỉ thực hiện sau M30 local pass.

Mặc định khuyến nghị của kế hoạch:

- không dùng 40 mm làm ngưỡng sản phẩm khi chưa qua calibration;
- giữ raw safety độc lập với AI;
- dùng backend event đã xác nhận làm nguồn blockchain;
- dùng local Hardhat cho bản demo cuối đầu tiên;
- dashboard chỉ được gọi hoàn tất khi đã hiển thị live data thật.
