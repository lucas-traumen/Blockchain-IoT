# Kế hoạch V2 — AI → luồng vận hành → blockchain → dashboard sản phẩm

> Trạng thái: **PROPOSED — chờ duyệt trước khi sửa source**
>
> Ngày lập: 2026-10-10
>
> Phạm vi: ESP32/HC-SR04, LSTM, iot_backend, blockchain_api, smart contracts,
> reward/session và dashboard.

Tài liệu này là kế hoạch triển khai tiếp theo của HRC Safety Log. Mục tiêu không
phải thêm nhiều chức năng rời rạc, mà biến các khối đang có thành một sản phẩm
demo hoàn chỉnh, có dataset mô phỏng tái lập được, có bằng chứng AI, có đường
ghi sự kiện lên blockchain và có dashboard hiển thị đúng dữ liệu trực tiếp.

Phạm vi AI của phiên bản này là **synthetic-only**: tự sinh dataset theo ma trận
kịch bản, huấn luyện và đánh giá hoàn toàn trên các session mô phỏng. Không có
hạng mục bắt buộc thu dataset từ HC-SR04 thật. Vì vậy mọi metric AI trong tài
liệu chỉ chứng minh chất lượng trên phân phối mô phỏng, không được diễn giải
thành độ an toàn hoặc độ chính xác ngoài thực tế.

Tài liệu này không thay thế lịch sử trong [PLAN.md](../PLAN.md). Khi kế hoạch
được duyệt, từng milestone bên dưới phải được đưa vào PLAN.md với trạng thái,
task key, file được phép sửa, lệnh kiểm tra và evidence tương ứng.

---

## 1. Kết luận điều hành

Hệ thống hiện đã có đủ khối phần mềm cơ bản, nhưng chưa nên gọi là sản phẩm hoàn
chỉnh vì còn bốn khoảng trống quan trọng:

1. Model hiện có cho kết quả tốt trên bộ synthetic cũ nhưng chưa có một bộ sinh
   dữ liệu V2 đủ bao phủ, bất biến và chống leakage để kết luận chắc chắn.
2. Ngưỡng 40 mm sẽ là tham số của thế giới mô phỏng. Nó không còn phụ thuộc gate
   hiệu chuẩn HC-SR04, nhưng cũng không được coi là ngưỡng phần cứng đã xác minh.
3. Sự kiện VIOLATION đã được lưu trong SQLite, nhưng chưa có cầu nối chuẩn từ
   event_packages sang POST /api/safety-event và HRCSafetyLog.
4. Dashboard hiện đọc chủ yếu dữ liệu blockchain lịch sử; biểu đồ khoảng cách,
   dự báo 100–500 ms và trạng thái realtime vẫn là placeholder.

Do đó thứ tự triển khai bắt buộc là:

1. chốt đặc tả thế giới mô phỏng và ma trận kịch bản;
2. sinh dataset V2, khóa split và sửa khả năng cảnh báo của AI;
3. khóa luồng runtime và cơ chế xác nhận sự kiện;
4. thêm outbox ghi sự kiện an toàn lên blockchain;
5. cuối cùng mới nối dashboard realtime và hoàn thiện trải nghiệm sản phẩm.

Không được đảo thứ tự bằng cách làm dashboard đẹp trước khi dữ liệu và trạng
thái phía dưới đã có hợp đồng rõ ràng.

---

## 2. Trạng thái hiện tại và khoảng trống

| Khối | Đã có | Khoảng trống phải xử lý |
|---|---|---|
| Phần cứng | ESP32/ESP32-S3 + HC-SR04, đo 100 ms, MQTT QoS1 | Chỉ dùng cho live demo tùy chọn; không dùng làm nguồn train hoặc bằng chứng chất lượng AI V2 |
| Telemetry | telemetry_v1, device/boot/sequence, dedup, SQLite WAL | Payload raw không mang kết quả Edge LSTM; chưa có schema riêng cho edge prediction |
| Safety | CLEAR/VIOLATION/UNKNOWN, Sp, margin | Cần tách rõ VIOLATION đo tức thời với VIOLATION dùng để phạt; 40 mm chỉ là tham số benchmark mô phỏng |
| AI backend | LSTM input 10×2, output 5 khoảng cách 100–500 ms | Cần synthetic V2 có kịch bản khó, split theo session/seed và metric ở cấp episode |
| AI edge | LSTM float32 đã sinh vào firmware, có parity native | Chưa flash/soak đầy đủ; chưa gửi prediction có model hash về backend |
| Event | Debounce warning 2/3, event_packages, raw pre/post | Thiếu log quan sát; thiếu trạng thái confirmation riêng cho vi phạm nhiễu |
| Blockchain safety | HRCSafetyLog và POST /api/safety-event | Chưa tự động lấy event_packages và anchor từng event |
| Blockchain reward | SessionRewards, stake/reward/penalty, durable reward outbox | Cần liên kết rõ với VIOLATION_CONFIRMED, không phạt vì một spike |
| Dashboard | Event, transaction, reward, robot lock/unlock, MetaMask | Realtime chart/forecast chưa hoạt động; URL/contract còn phụ thuộc local/hard-code |
| Vận hành | Docker MQTT + iot_backend; chain/API chạy riêng | Chưa có một lệnh dựng toàn bộ stack và health tổng hợp |

### 2.1 Sự thật phải giữ nguyên trong báo cáo

- Kết quả synthetic tốt chỉ chứng minh pipeline có thể học và tổng quát trong
  không gian mô phỏng đã khai báo; không thay thế kết quả từ ESP32/HC-SR04 thật.
- Confusion matrix là đầu ra đánh giá từ ground truth mô phỏng và dự đoán của model;
  không phải công cụ sinh dữ liệu. Dataset được sinh bằng ma trận kịch bản và bộ
  tham số chuyển động/nhiễu có seed.
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

- Raw measurement: telemetry_v1 trong raw_samples; mặc định do simulator phát,
  tùy chọn do ESP32 phát khi demo live.
- Ground truth synthetic: dataset release bất biến; chỉ pipeline tạo nhãn/eval
  được đọc, runtime inference và blockchain không được dùng trường này.
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
| square-bench-v2 | Benchmark synthetic và demo luồng | Sp cố định 0,04 m trong thế giới mô phỏng |

Với phạm vi synthetic-only, 40 mm là một tham số toán học dùng để tạo nhãn và
kiểm thử crossing. Mọi payload, manifest, model card và dashboard phải hiển thị
`simulation_mode=true` và `threshold_verified_on_hardware=false` khi dùng profile
này. Cấm suy ra rằng HC-SR04 đã đo chính xác ở 40 mm chỉ vì model đạt metric.

Nếu sau này nhóm muốn chuyển sang tuyên bố chạy thật, đó là milestone độc lập:
phải hiệu chuẩn cảm biến, chọn lại vùng vận hành, tạo dataset thật và đánh giá
domain shift. Milestone tương lai đó không chặn bản demo synthetic hiện tại.

---

## 4. Kiến trúc đích

~~~mermaid
flowchart TD
    A["Simulator mặc định<br/>ESP32 tùy chọn"] --> B["MQTT QoS1<br/>telemetry + prediction"]
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
    participant S as Simulator hoặc ESP32
    participant M as MQTT
    participant I as IoT backend
    participant B as Blockchain API
    participant C as Contracts
    participant D as Dashboard

    S->>S: Sinh/đo sample + kiểm tra ngưỡng
    S->>S: Edge LSTM tùy chọn dự báo 100–500 ms
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

Simulator phải phát đúng schema trên, dùng device_id có prefix sim-, và kèm
source/simulation_mode ở envelope hoặc registry thiết bị. distance_true_m tuyệt
đối không được phát trong telemetry runtime vì đây là nhãn đặc quyền.

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
  "simulation_trained": true,
  "model_version": "lstm-v2",
  "model_sha256": "...",
  "scaler_sha256": "...",
  "preprocessing_version": "pp-lstm-v2",
  "threshold_m": 0.04,
  "horizons_ms": [100, 200, 300, 400, 500],
  "distance_m": [0.080, 0.065, 0.052, 0.039, 0.028],
  "predicted_violation_ms": 400,
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
    "training_data": "synthetic-only",
    "dataset_sha256": "...",
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

### 6.2 Gate AI-0 — khóa hợp đồng mô phỏng trước khi train

Không dùng confusion matrix để sinh dữ liệu. Confusion matrix chỉ được tính sau
khi model dự đoán trên tập có nhãn. Nguồn sinh dữ liệu là một simulator có seed
và ma trận kịch bản được version hóa.

Hợp đồng mô phỏng V2:

| Thuộc tính | Giá trị mặc định | Quy tắc |
|---|---:|---|
| Chu kỳ mẫu | 100 ms | 10 Hz, có jitter riêng nếu kịch bản yêu cầu |
| Input window | 10 mẫu | 1 giây lịch sử, không vượt session boundary |
| Forecast horizon | 100, 200, 300, 400, 500 ms | Năm output cố định |
| Ngưỡng Sp | 0,04 m | Chỉ là ngưỡng benchmark synthetic |
| Đơn vị chuẩn | m, s, m/s, m/s² | Không trộn mm vào tensor |
| Ground truth | distance_true_m | Chỉ dùng tạo nhãn và audit |
| Model input | distance_measured_m và feature nhân quả | Không chứa future/label |
| Randomness | PRNG có seed/session | Mọi session tái sinh được |
| Chế độ | simulation_mode=true | Hiển thị xuyên suốt API và dashboard |

Deliverable của AI-0:

- configs/simulation/square-bench-v2.yaml;
- schema generator và schema sample;
- scenario_catalog.json;
- lệnh dry-run sinh 10 session;
- test cùng config + seed tạo cùng dữ liệu canonical và SHA-256;
- kiểm tra đơn vị, monotonic timestamp, sequence và boundary.

### 6.3 Bộ sinh quỹ đạo và mô hình phép đo

Mỗi session có một quỹ đạo ẩn d_true(t). Simulator tạo số đo d_measured(t) từ
quỹ đạo đó. Nhờ tách hai lớp, model học từ tín hiệu có nhiễu nhưng nhãn vẫn có
ground truth chính xác.

Các họ quỹ đạo bắt buộc:

- stationary: d_true(t) = d0;
- approach: d_true(t) = max(d_min, d0 − v·t − 0,5·a·t²);
- recede: d_true(t) = d0 + v·t + 0,5·a·t²;
- oscillation: d_true(t) = Sp + b + A·sin(2πf·t + φ);
- stop-before: approach theo từng đoạn, giảm tốc rồi dừng tại Sp + margin;
- sudden-step: giữ ổn định rồi đổi khoảng cách trong một hoặc hai chu kỳ;
- mixed-motion: nhiều đoạn approach/hold/recede nhưng vẫn thuộc một session.

Mô hình đo V2:

~~~text
d_measured(t) =
  quantize(d_true(t) + bias + gaussian_noise + correlated_drift)
~~~

Sau đó mới áp dụng fault độc lập:

- spike dương/âm;
- dropout hoặc timeout;
- frozen value;
- timestamp jitter;
- sequence gap;
- burst noise.

Mọi tham số phải nằm trong config, không hard-code trong generator. Tối thiểu
gồm d0, duration, v, a, b, A, f, φ, bias, noise_sigma, drift_sigma,
quantization_mm, spike_probability, dropout_probability, jitter_ms và seed.
Generator phải từ chối cấu hình phi vật lý như khoảng cách âm hoặc timestamp
không tăng.

### 6.4 Ma trận kịch bản Synthetic Dataset V2

Mục tiêu là 1.200 session forecast và 150 session fault. Con số này có thể tăng,
nhưng tỷ lệ và support test không được giảm.

| Nhóm | Session | Nhãn chính | Biến thiên bắt buộc |
|---|---:|---|---|
| Stationary CLEAR | 120 | Negative | d0 xa/gần Sp, bias, drift |
| Stationary near-threshold | 120 | Negative và observed | Hai phía Sp, noise biên |
| Approach crossing chậm | 120 | Positive | v thấp, nhiều d0 và lead time |
| Approach crossing vừa | 120 | Positive | v trung bình, a âm/dương nhẹ |
| Approach crossing nhanh | 120 | Positive | v cao nhưng vẫn có ≥10 input mẫu |
| Stop-before-Sp | 180 | Hard negative | margin 1–30 mm, giảm tốc khác nhau |
| Recede | 100 | Negative | Bắt đầu gần Sp rồi đi xa |
| Oscillation quanh Sp | 160 | Mixed/hard | A, f, phase, nhiều lần crossing |
| Sudden step | 80 | Mixed | Bước 1–2 chu kỳ, có/không crossing |
| Mixed acceleration | 80 | Mixed | Approach/hold/recede theo đoạn |
| Fault-only, tách riêng | 150 | UNKNOWN | dropout, spike, frozen, gap |

Sampling tham số phải là phân phối, không phải vài giá trị cố định. Các vùng biên
được lấy dày hơn nhưng vẫn giữ đủ vùng bình thường:

- d0: 0,045–1,0 m;
- tốc độ approach: 0,02–0,80 m/s;
- gia tốc: −0,60 đến 0,60 m/s²;
- stop margin: 0,001–0,030 m;
- noise sigma: 0–0,008 m;
- bias: −0,005 đến 0,005 m;
- jitter: 0–20 ms;
- session duration: 12–40 s.

Các khoảng trên là hợp đồng mô phỏng, không phải thông số đã đo từ HC-SR04.
Mỗi session chỉ chứa một chuỗi thời gian độc lập và có metadata:

- dataset_version, generator_version, generator_commit;
- session_id, scenario, scenario_parameters;
- seed, split, simulation_mode=true;
- sample_period_ms, Sp, safety_profile;
- noise/fault parameters;
- start/end timestamp mô phỏng;
- expected crossing episode count;
- quality status và rejection reason;
- config SHA-256 và session payload SHA-256.

### 6.5 Schema dữ liệu và chống rò rỉ nhãn

Mỗi sample phải giữ ít nhất:

| Trường | Dùng ở đâu | Được đưa vào model? |
|---|---|---|
| session_id, sequence, measured_at | boundary/audit | Không |
| distance_true_m | tạo target và nhãn | Không |
| distance_measured_m | input gốc | Có |
| delta_distance_m | input dẫn xuất nhân quả | Có |
| sensor_valid, fault_code | quality/fault | Có nếu experiment khai báo |
| Sp, scenario, seed, split | metadata/slice | Không trong model V2 |
| crossing_h100…h500 | label classification | Không |
| target_d100…d500 | label regression | Không |

Quy tắc nhân quả:

- feature tại t chỉ được dùng sample có timestamp ≤ t;
- rolling feature phải reset ở đầu session, gap và invalid boundary;
- scaler chỉ fit trên train;
- không nội suy qua gap/dropout;
- không dùng scenario, seed, split hoặc distance_true_m làm feature;
- không shuffle sample trước khi tạo window;
- raw synthetic JSONL bất biến; window dataset là artifact dẫn xuất.

Audit bắt buộc chạy trước train:

- schema và range validation;
- timestamp/sequence tăng đúng;
- không trùng session_id hoặc seed giữa split;
- không trùng SHA-256 payload session;
- tỷ lệ scenario và nhãn theo từng split;
- số window bị loại theo lý do;
- xác nhận không có feature tương lai;
- tái sinh ngẫu nhiên ít nhất 20 session và so hash.

### 6.6 Chia train/validation/test đúng theo session

Split được gán **trước khi windowing** và khóa trong split_manifest.json:

| Split | Tỷ lệ | Mục đích |
|---|---:|---|
| Train | 70% session mỗi scenario | Fit scaler, weight và model |
| Validation | 15% session mỗi scenario | Chọn architecture, threshold, debounce |
| Test | 15% session mỗi scenario | Chỉ chạy khi candidate đã khóa |

Quy tắc bắt buộc:

1. Toàn bộ window của một session chỉ thuộc đúng một split.
2. Seed và session_id không được tái sử dụng giữa các split.
3. Stratify theo scenario và crossing support, không stratify bằng từng window.
4. Train có thể weighted-sample positive/hard-negative; validation và test giữ
   phân phối nguyên bản.
5. Không oversample, augment hoặc hard-mine trên test.
6. Test manifest được ghi hash trước lần train chính thức.
7. Sau khi xem test, mọi chỉnh sửa generator/model tạo experiment version mới;
   không âm thầm chạy lại để chọn kết quả đẹp nhất.
8. Fault-only dataset có manifest riêng; không biến UNKNOWN thành negative.

Ngoài test cùng phân phối, tạo một challenge set độc lập từ seed chưa từng dùng,
có noise cao, stop margin 1–5 mm và tham số ở rìa. Challenge set dùng để báo cáo
độ bền, không dùng để chọn hyperparameter và không thay thế test chính.

### 6.7 Tạo nhãn forecast và early warning

Với window kết thúc ở thời điểm t:

- regression target là d_true(t+100)…d_true(t+500);
- crossing_hK = 1 khi d_true(t) > Sp và tồn tại d_true trong (t, t+K] ≤ Sp;
- window có d_true(t) ≤ Sp là current violation, không phải positive
  early-warning;
- window thiếu future horizon không được pad và phải loại có lý do;
- window chứa invalid/gap theo policy được gắn INVALID_WINDOW, không gán 0;
- các crossing liên tục được gom thành một episode bằng crossing_id.

Một bản kiểm tra nhãn độc lập phải tính lại label trực tiếp từ quỹ đạo ẩn và so
khớp 100%. Kiểm thử biên phải có trường hợp đúng tại Sp, ngay trên/dưới Sp và
crossing đúng ở 100/200/300/400/500 ms.

### 6.8 Confusion matrix dùng đúng chỗ

Confusion matrix được tạo từ nhãn test và prediction sau khi candidate, scaler,
probability threshold và detector 2-of-3 đã bị khóa.

| Ô | Window-level | Episode/session-level |
|---|---|---|
| TP | Dự báo crossing và label = 1 | Có warning hợp lệ 100–500 ms trước crossing |
| FP | Dự báo crossing nhưng label = 0 | Có warning trong negative episode/session |
| TN | Không dự báo và label = 0 | Negative episode/session không có warning |
| FN | Không dự báo nhưng label = 1 | Crossing không có warning hợp lệ |

Episode-level là metric chính. Mỗi crossing chỉ được ghép tối đa một warning;
nhiều warning liên tiếp được collapse thành một episode. Báo cáo phải có cả ma
trận số lượng tuyệt đối và ma trận chuẩn hóa, không chỉ accuracy.

Confusion matrix được dùng để tìm nhóm lỗi theo scenario, tốc độ, noise, margin
và horizon. Nó có thể dẫn tới **Synthetic V3**, nhưng quy trình phải là:

~~~text
scenario matrix V2
  → dataset/split V2 đã khóa
  → train + validation
  → test V2 một lần
  → confusion matrix + error slices
  → thiết kế generator V3
  → dataset/split/test V3 mới
~~~

Không sao chép FN/FP từ test V2 vào train V2. Nếu cần hard-negative mining, chỉ
mine từ train/validation hoặc sinh session mới bằng seed mới cho phiên bản sau.

### 6.9 Tiền xử lý và experiment

Thử nghiệm feature theo thứ tự:

1. E0: distance_measured_m + delta_distance_m như hiện tại;
2. E1: thêm velocity estimate và rolling robust slope;
3. E2: thêm recent valid mask, jitter và fault-count nhân quả;
4. chỉ giữ feature nếu validation cải thiện và firmware/backend tái tạo giống
   nhau.

Mỗi experiment chỉ thay một nhóm yếu tố và phải ghi:

- experiment_id;
- dataset/split/config hash;
- feature set;
- architecture và hyperparameter;
- training seed;
- class/sample weight;
- threshold/debounce;
- best validation epoch;
- test_evaluated=false cho đến khi candidate bị khóa.

Baseline bắt buộc:

- persistence: mọi horizon bằng khoảng cách cuối;
- constant velocity;
- threshold-only không AI;
- LSTM V1 hiện tại.

Bộ synthetic hiện có square-40mm-20261010 chỉ là baseline tham khảo: 400
session, 96.000 sample, split 280/60/60; test có 13.560 window và 51 crossing.
Kết quả đã ghi nhận trước debounce F1 0,9375; sau detector 2-of-3 precision 1,0,
recall 0,6471, F1 0,7857 và miss rate 0,3529. Synthetic V2 phải giải quyết trực
tiếp mức recall sau debounce này.

### 6.10 Chiến lược model

#### Bước AI-1 — LSTM regression tương thích hiện tại

- input 10×2 cho E0;
- LSTM 32;
- Dropout 0,2;
- Dense 32 ReLU;
- Dense 5 khoảng cách;
- weighted Huber cho near-threshold và pre-crossing window;
- threshold prediction tại Sp rồi chạy detector 2-of-3.

Giữ model này nếu đạt cả regression gate và episode gate.

#### Bước AI-2 — multi-task LSTM nếu AI-1 bỏ sót crossing

- shared LSTM encoder;
- head A dự báo năm khoảng cách;
- head B dự báo năm xác suất crossing;
- loss = weighted Huber + λ × weighted binary cross-entropy;
- λ, class weight và threshold chỉ chọn bằng validation;
- predicted_violation_ms là horizon sớm nhất vượt threshold đã khóa.

Persistence và constant velocity chỉ là baseline offline, không được dùng để
điều chỉnh nhãn cho có lợi cho LSTM.

### 6.11 Metric và báo cáo bắt buộc

#### Regression

- MAE/RMSE/bias cho từng horizon;
- p50/p95 absolute error;
- MAE near-threshold;
- so sánh persistence và constant velocity;
- slice theo scenario, speed, acceleration, noise và stop margin.

#### Classification/event

- confusion matrix window-level trước debounce;
- confusion matrix episode-level sau detector 2-of-3;
- precision, recall, F1, miss rate, specificity;
- false warning trên mỗi negative session và mỗi phút;
- lead time p10/p50/p90;
- support tuyệt đối của positive, negative và từng scenario.

#### Độ tin cậy

- bootstrap confidence interval 95% theo session, không bootstrap window;
- metric trên challenge set;
- chênh lệch train/validation/test;
- ba training seed và mean ± standard deviation cho candidate cuối.

#### Runtime

- inference latency p50/p95/p99;
- stale và INVALID_WINDOW rate;
- edge/backend parity nếu bật Edge AI;
- heap/stack/flash và ảnh hưởng chu kỳ 100 ms nếu deploy ESP32-S3.

### 6.12 Gate chấp nhận AI synthetic

| Tiêu chí trên test synthetic đã khóa | Gate tối thiểu |
|---|---:|
| Crossing episode support | ≥ 100 |
| Negative session/episode support | ≥ 100 |
| Recall sau debounce | ≥ 0,85 |
| Precision sau debounce | ≥ 0,95 |
| F1 sau debounce | ≥ 0,89 |
| Miss rate | ≤ 0,15 |
| False warning | ≤ 0,10/session âm tính |
| Lead time median | ≥ 200 ms |
| Lead time p10 | ≥ 100 ms |
| MAE tại 500 ms | ≤ 5 mm |
| Near-threshold MAE | Không tệ hơn best baseline và mục tiêu tốt hơn ≥5% |
| Session leakage | 0 |
| Reproducibility | Cùng config/seed tạo cùng dataset hash |
| Edge inference p95, nếu deploy | < 50 ms |
| Edge/backend parity, nếu deploy | Sai lệch tối đa ≤ 0,1 mm |

Pass gate chỉ cho phép nhãn **SYNTHETIC_VALIDATED**. Dashboard vẫn phải hiện
“Mô hình được huấn luyện và đánh giá bằng dữ liệu mô phỏng; chưa xác minh trên
phần cứng/thế giới thật”. Không dùng chữ certified, production-safe hoặc
hardware-validated.

### 6.13 Artifact và promotion

Mỗi dataset release phải có:

- dataset.jsonl hoặc session files bất biến;
- generator_config.yaml;
- scenario_catalog.json;
- dataset_manifest.json;
- split_manifest.json;
- audit_report.json;
- SHA-256 toàn dataset, từng split và config.

Mỗi model artifact phải có:

- model.keras;
- scaler.json;
- manifest.json;
- metrics.json;
- confusion_matrix_window.json;
- confusion_matrix_episode.json;
- predictions_test.jsonl;
- model/scaler/dataset/split SHA-256;
- source commit, TensorFlow/Keras version và training seed;
- architecture, features, units, horizons, threshold/debounce;
- acceptance status và limitations.

Promotion trong phạm vi này:

~~~text
candidate → synthetic_validated → shadow_runtime_optional → demo_active
~~~

Không ghi đè artifact active. Chỉ đổi symlink/env sau khi hash, audit, gate và
model card pass. hardware_validated không tồn tại trong phạm vi synthetic-only.

### 6.14 Triển khai Edge AI tùy chọn

Edge AI không phải điều kiện để chứng minh metric synthetic. Nếu vẫn cần demo
ESP32:

1. sinh source weights/scaler từ artifact demo_active;
2. kiểm tra hash trong firmware;
3. parity test tối thiểu 100 vector synthetic gồm near-threshold và outlier;
4. build/flash và ghi serial model hash, latency;
5. phát edge_prediction_v1 với simulation_trained=true;
6. backend đối chiếu edge/backend và ghi parity_status;
7. dashboard gắn nhãn EXPERIMENTAL trên dữ liệu sensor live.

Việc chạy sensor thật trong demo không biến telemetry đó thành dataset train và
không thay đổi trạng thái SYNTHETIC_VALIDATED. Nếu inference làm trễ chu kỳ đo,
ưu tiên raw sampling, chạy task riêng và bỏ prediction cũ thay vì bỏ raw sample.

---

## 7. Luồng runtime chi tiết

### 7.1 Startup

1. Chọn source_mode=simulator mặc định hoặc source_mode=esp32 tùy chọn.
2. Nguồn dữ liệu tạo boot_id mới; simulator load scenario/seed, ESP32 khởi tạo
   sensor/Wi-Fi.
3. In model_version, model hash, scaler hash, threshold profile.
4. Backend mở SQLite, chạy migrations và load safety/model.
5. Backend catch-up raw chưa xử lý trước khi nhận stream mới.
6. MQTT subscribe thành công mới đánh dấu control ready.
7. Dashboard chỉ báo ONLINE khi cả live timestamp và backend health còn mới.

### 7.2 Xử lý mỗi mẫu

1. Simulator sinh hoặc ESP32 đo sample rồi chốt monotonic_us/measured_at.
2. Kiểm tra sample/sensor validity.
3. So raw distance với threshold cục bộ để fail-safe.
4. Đẩy sample vào Edge LSTM nếu source có edge runtime và window hợp lệ.
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
| Simulator dừng | Không có sample mới | Không giả dữ liệu | Không ảnh hưởng outbox cũ | STALE/DISCONNECTED |
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

Toàn trang phải có badge nguồn dữ liệu: SYNTHETIC STREAM hoặc HARDWARE
EXPERIMENTAL. Khi model được train synthetic-only, banner limitation luôn hiện
ở Overview và AI Quality, kể cả khi input runtime đến từ ESP32.

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
- source mode, scenario và seed nếu là simulator;
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
- training_data=synthetic-only và limitation banner;
- generator/config/scenario catalog version;
- dataset/split hash;
- MAE/RMSE từng horizon;
- precision/recall/F1 sau debounce;
- confusion matrix window-level và episode-level;
- bộ lọc error slice theo scenario/noise/speed/margin;
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

### M22 — Đặc tả simulator và safety profile synthetic

Chủ trì: TV4. Phối hợp: TV3.

Phạm vi:

- configs/simulation/square-bench-v2.yaml;
- profile square-bench-v2 với Sp = 0,04 m;
- schema session/sample và scenario catalog;
- deterministic PRNG, trajectory/fault generator;
- validator và test nhãn biên;
- simulation provenance trên API/dashboard.

Deliverable:

- generator chạy CLI được;
- 10 golden sessions có hash cố định;
- tài liệu công thức, đơn vị và giới hạn phân phối;
- profile hrc-ssm-v1 và square-bench-v2 không bị trộn semantics;
- dashboard label synthetic/non-certified.

DoD:

- cùng config/seed sinh cùng canonical hash;
- ground truth tách khỏi measured input;
- test Sp/margin/label 100–500 ms pass;
- không có feature nhìn tương lai;
- 40 mm được ghi rõ là ngưỡng mô phỏng, không phải calibration phần cứng.

### M23 — Synthetic Dataset V2 và split manifest

Chủ trì: TV4.

Phạm vi:

- sinh đủ ma trận 1.200 forecast + 150 fault session;
- metadata/session/sample schema;
- split 70/15/15 trước windowing;
- quality/leakage audit;
- frozen test và challenge manifests.

DoD:

- đạt support theo từng scenario;
- train/validation/test không trùng session, seed hoặc payload hash;
- test crossing và negative support đủ;
- dataset/config/split hash và provenance hoàn chỉnh;
- raw dataset bất biến, derived windows tái tạo được;
- fault dataset tách riêng và UNKNOWN không bị gán negative.

### M24 — LSTM V2 và metric event

Chủ trì: TV4.

Pha:

1. retrain architecture hiện tại với balanced/weighted loss;
2. khóa candidate bằng validation và đánh giá sau detector 2-of-3;
3. chỉ khi fail mới multi-task LSTM;
4. calibration probability threshold trên validation;
5. chạy frozen test một lần, xuất hai confusion matrix;
6. artifact promotion.

DoD:

- metric regression/window/episode đầy đủ;
- confusion matrix trước và sau debounce có số tuyệt đối;
- đạt gate synthetic hoặc ghi rõ fail;
- không chọn lại test split/model theo kết quả test;
- ba training seed và confidence interval theo session;
- baseline offline được báo cáo trung thực;
- artifact candidate không ghi đè active.

### M25 — Runtime inference và Edge AI tùy chọn

Chủ trì: TV3 + TV4.

Phạm vi:

- backend inference từ artifact demo_active;
- prediction schema có simulation_trained/model/dataset hash;
- edge_prediction_v1 nếu cần demo firmware;
- backend ingest/store/parity;
- generator model weights và native test.

DoD:

- backend inference/replay synthetic pass;
- 100 parity vectors pass nếu bật Edge AI;
- model/scaler hash khớp;
- inference p95 < 50 ms và sampling 100 ms không trễ nếu deploy edge;
- dashboard hiển thị SYNTHETIC_VALIDATED/EXPERIMENTAL đúng nguồn;
- chạy sensor thật, nếu có, không được đổi nhãn thành hardware-validated.

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
- số liệu lấy từ API runtime, không dùng placeholder hard-code;
- live latency dưới 1 giây;
- state/stale/source rõ ràng;
- contract/network không hard-code;
- browser E2E pass.

### M30 — E2E synthetic acceptance và demo bundle

Chủ trì: cả nhóm.

Kịch bản:

1. simulator phát session CLEAR;
2. simulator phát approach tạo PREDICTED_WARNING;
3. synthetic crossing tạo VIOLATION_OBSERVED rồi CONFIRMED;
4. chain tắt rồi phục hồi;
5. duplicate MQTT;
6. fault injection;
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
| M22 | 3–5 ngày công | Đặc tả synthetic |
| M23 | 3–6 ngày công | M22 |
| M24 | 5–8 ngày công | M23 |
| M25 | 2–5 ngày công | M24; edge là tùy chọn |
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

| Lớp | Unit | Integration | E2E/Runtime |
|---|---|---|---|
| Simulator/source | trajectory/noise/fault/seed | telemetry schema + replay | deterministic scenario stream |
| Safety | Sp/margin/state | config version/replay | synthetic crossing/fault |
| AI | window/scaler/loss/metric | artifact/runtime/parity | confusion matrix + event recall |
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
ESP-IDF build ESP32-S3 nếu bật Edge AI
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
   simulator, reverse proxy và dashboard bằng một compose profile.
2. Health tổng hợp phải xanh.
3. Dashboard hiển thị device chưa online cho tới khi có telemetry mới.
4. Simulator công bố dataset/scenario/seed/model/config hash. Nếu dùng ESP32,
   thiết bị công bố thêm firmware hash và giao diện phải gắn nhãn EXPERIMENTAL.

### 15.2 Session an toàn

1. Operator START session.
2. Stake 2 HRC chuyển sang started.
3. Simulator phát quỹ đạo stationary ngoài Sp.
4. Dashboard vẽ actual/forecast, trạng thái CLEAR.
5. STOP sau đủ duration/coverage.
6. Outcome VALID_NO_VIOLATION.
7. Contract hoàn stake và thưởng theo daily policy.
8. Dashboard hiện evidence hash, tx và balance.

### 15.3 Cảnh báo rồi vi phạm

1. START session mới.
2. Simulator phát quỹ đạo approach từ vùng CLEAR.
3. AI tạo warning đủ 2/3 trước crossing.
4. Dashboard chuyển vàng và hiển thị lead time.
5. Quỹ đạo vượt Sp: local stop/cảnh báo ngay.
6. Sau 2/3 mẫu valid, event VIOLATION được confirmed.
7. Event local chuyển pending rồi on-chain confirmed.
8. STOP session; outcome VIOLATION_CONFIRMED.
9. Contract thu stake/penalty đúng policy.

### 15.4 Fault injection

1. Simulator tạo timeout/dropout theo seed đã ghi.
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

### Simulator/IoT

- generator/config/seed tạo lại đúng dataset hash;
- simulator phát telemetry_v1 10 Hz ổn định;
- scenario, source_mode và simulation_mode truy vết được;
- reconnect, duplicate, gap và fault injection pass;
- Edge AI/ESP32, nếu bật, không làm mất deadline 100 ms.

### AI

- frozen synthetic test đủ support;
- train/validation/test tách theo session và seed, leakage bằng 0;
- đạt gate precision/recall/F1/lead time sau debounce;
- hai confusion matrix và error slices được xuất;
- artifact/hash/split/scaler/generator audit pass;
- edge/backend parity pass nếu bật Edge AI;
- dashboard hiện đúng SYNTHETIC_VALIDATED và limitation.

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

- realtime data từ simulator/API runtime, không placeholder hard-code;
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
- generator không tái lập được cùng hash;
- session/seed/payload bị trùng giữa các split;
- confusion matrix thiếu support hoặc được tính trước khi khóa threshold;
- F1/recall không đạt;
- edge/backend lệch vượt tolerance nếu bật Edge AI;
- AI làm trễ sampling/runtime stream;
- event duplicate on-chain;
- reward có thể settle hai lần;
- dashboard chứa key/token;
- chain outage làm dừng safety.

Rollback:

- đổi MODEL_ARTIFACT_DIR về artifact active trước;
- firmware, nếu dùng, quay về model hash trước;
- giữ safety trực tiếp hoạt động;
- pause chain worker nhưng không xóa outbox;
- không reset DB/contract để che lỗi;
- ghi rõ version và lý do rollback.

---

## 18. Thứ tự phê duyệt

Trước khi triển khai source, nhóm cần duyệt năm quyết định:

1. Chấp nhận phạm vi AI synthetic-only và Sp = 40 mm chỉ là benchmark mô phỏng.
2. Chốt ma trận 1.200 forecast + 150 fault session và split 70/15/15.
3. Chấp nhận backend là authority/audit; Edge AI chỉ là tùy chọn demo.
4. Chỉ dùng VIOLATION_CONFIRMED cho penalty.
5. Hoàn tất local Hardhat trước; Sepolia chỉ thực hiện sau M30 local pass.

Mặc định khuyến nghị của kế hoạch:

- không thu dataset thật trong phạm vi V2;
- hiển thị rõ 40 mm chưa được xác minh trên phần cứng;
- giữ raw safety độc lập với AI;
- dùng backend event đã xác nhận làm nguồn blockchain;
- dùng local Hardhat cho bản demo cuối đầu tiên;
- dashboard chỉ được gọi hoàn tất khi hiển thị live data từ simulator/API và
  nhãn synthetic limitation, không dùng placeholder.
