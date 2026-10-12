# PROJECT_MEMORY — quyết định bền vững

Repo: HRC Safety Log (Blockchain-IoT). Phạm vi runtime gồm TV3 + TV4 và demo
TV1/TV2 reward bridge/dashboard local. Smart Contract, hash, dashboard và bộ gom
sự kiện có API riêng; IoT không giữ private key và không tự ký giao dịch.

## Kiến trúc chốt

- Luồng: ESP32 + HC-SR04 → MQTT (Mosquitto) → Python iot_backend (FastAPI + paho callback/raw commit + worker compute) → SQLite WAL (`data/hrc.db`) → safety + inference → `processed_samples` (bàn giao TV2 qua REST/export).
- Không dùng ROS2, không robot 3D, không tay đòn cơ khí. AI chạy trên iot_backend; ESP32 chỉ đo và truyền.
- Backend Python thống nhất (không dựng Node trùng chức năng — repo khởi đầu trống nên chọn một stack).
- Chạy từ repo root, không cài package: `iot_backend`, `ai_model`, `scripts` là package top-level. Venv `.venv/` là lựa chọn Python trực tiếp.
- Người dùng chọn Docker cho host service (2026-10-02): Compose `blockchain-hrc`
  quản lý Mosquitto1885/API8001, iot_backend dùng mqtt:1883 trong mạng Docker;
  named volumes giữ SQLite và broker. RF image không TF; AI profile/image
  phục vụ train/evaluate và iot_backend LSTM khi chọn tường minh.

## Quy ước dữ liệu

- Khóa mẫu: `(device_id, boot_id, sequence)`; UNIQUE trong `raw_samples` và `processed_samples` (+ `processor_version`).
- Đơn vị nội bộ SI (m, s, m/s); payload telemetry dùng mm/µs; đổi ở biên.
- Timestamp: `measured_at` ISO-8601 UTC (ms) tại lúc bắt đầu đo; chưa sync SNTP → null + `time_synced=false`. `monotonic_us` để tính khoảng thời gian giữa mẫu.
- Topic: `hrc/v1/devices/{device_id}/telemetry|control|ack`, QoS 1, retain=false.
- Session: khóa bằng ack từ thiết bị (boot_id + sequence tại lệnh start); fallback: max sequence đã nhận. Gán mẫu theo khoảng sequence cùng boot. Không gán theo giờ nhận. Trong lúc START/STOP chờ ACK, SessionManager giữ boundary gate; MQTT chỉ commit raw đồng bộ rồi worker mới compute, để assessment/predictor/event cùng scope.
- Schema version "1.0" cho telemetry/assessment/control/ack; file JSON Schema ở `schemas/` là nguồn chân lý, DDL SQLite ở `iot_backend/ingestion/schema.sql`.

## Safety & model

- Công thức Sp chuẩn (SSM một chiều, robot phanh đều): `Sh=vh(Tr+Ts)`, `Sr=vr·Tr`, `Ss=vr·Ts/2`, `Sp=Sh+Sr+Ss+C+Zd+Zr`. Tr/Ts là tham số robot giả định, không lấy độ trễ Blockchain. Fixture: vh=1, vr=0.2, Tr=0.1, Ts=0.2, C=0.05, Zd=0.03, Zr=0.02 → Sp=0.44.
- Trạng thái: margin>0 → CLEAR, ≤0 → VIOLATION, lỗi → UNKNOWN (không bao giờ "nâng" UNKNOWN thành CLEAR, không tự giảm Sp).
- Cấu hình safety có `config_version` + `valid_from`; replay dùng config đúng thời điểm mẫu (fallback `received_at` khi chưa sync, ghi `config_time_source`).
- Baseline hiện tại: RandomForestRegressor multi-output, window 10 mẫu @100ms,
  horizons 100–500ms; feature chỉ khoảng cách quá khứ. RF được giữ làm legacy/
  rollback trong khi chuyển sang LSTM.
- LSTM đích dùng timestep `[distance_m, delta_distance_m]`, `delta[0]=0`, shape
  `(batch, 10, 2) → (batch, 5)`, TensorFlow 2.20.0/Keras 3.15.1. P0 đã
  kiểm chứng fit/predict/save/load với Python 3.12.3 + NumPy 2.5.3.
- Quyết định môi trường (M13 chốt 2026-10-02): `.venv` giữ RF/iot_backend không TF;
  environment AI riêng (`requirements-ai.txt`: TF2.20.0/Keras3.15.1 + iot_backend
  dependencies) chạy cả train/evaluate và iot_backend LSTM. Chọn `MODEL_TYPE=rf`
  mặc định hoặc `lstm` rõ ràng; không tự đổi sang RF khi LSTM hỏng.
- Contract M8: `configs/model.yaml` giữ RF legacy ở top-level và đặt config LSTM
  trong khối `lstm`; LSTM dùng `lstm_units=32`, Huber, Dense 32 ReLU và ba
  callback EarlyStopping/ReduceLROnPlateau/ModelCheckpoint. Không dùng key
  `units` cho số cell vì manifest dùng `units="m"` cho đơn vị khoảng cách.
- `assessment_v1` vẫn là schema 1.0 backward-compatible; hai field mới là
  optional `prediction.preprocessing_version` và
  `prediction.predicted_violation_ms`, field sau chỉ nhận 100/200/300/400/500
  hoặc null. `model_manifest_v1` bắt buộc SHA-256 của model, dataset và split.
- Windowing/feature dùng chung train + runtime qua `ai_model/windows.py` + `ai_model/features.py`.
- M9 chốt API LSTM trong `ai_model.features`: `window_to_lstm_features` sinh
  `[distance_m, delta_distance_m]` theo thứ tự cũ→mới, `delta[0]=0`, và
  `windows_to_lstm_array` trả `(N,10,2)`. Config feature name sai/swapped,
  block `lstm` malformed, window size null/lệch, NaN/inf/overflow đều raise;
  RF `d0..d9` adapter giữ nguyên. M10 đã dùng đúng API này, không inline
  preprocessing riêng.
- M10 đã hoàn tất lớp chuẩn bị dữ liệu trong `ai_model.prepare` và
  `ai_model.preprocessing`: `build_lstm_dataset` split theo session trước khi
  windowing, bảo vệ session boundary khi boot được tái sử dụng, giữ drop
  statistics và metadata cho breakdown; `LSTMSplit`/`LSTMDataset` giữ shape
  `(N,10,2)`/`(N,5)`. `LSTMScaler` chỉ fit trên train, serial hóa JSON với
  `fitted_on=train_only` và `preprocessing_version=pp-lstm-v1`; hash helpers
  dùng canonical JSON + SHA-256 cho dataset/split/scaler. Tổng kiểm chứng M10:
  targeted 53 pass, full suite 208 pass, smoke độc lập 9/9; không có blocker hay
  major sau review. Caller database/export vẫn chịu trách nhiệm cung cấp
  mapping có `session_id`; preprocessing module không phụ thuộc iot_backend/DB.
- M11a đã bổ sung `ai_model.lstm_train` với hai API độc lập `train_lstm` và
  `load_lstm_artifact`; không thay thế `ai_model.train` RF. TensorFlow được
  import lazy để iot_backend `.venv` vẫn chạy không cần TF. Trainer đọc khối
  `lstm` trong YAML, gọi M10 split-before-windowing, fit `LSTMScaler` chỉ trên
  train, train Input `(10,2)` → LSTM/Dropout/Dense → `(5)`, tính MAE sau
  inverse-transform về mét, rồi validate manifest/timestamp trước khi promote
  staging. Artifact bắt buộc và thứ tự kiểm tra loader: `model.keras`,
  `scaler.json`, `split_manifest.json`, `manifest.json`, `metrics.json`; hash
  model/scaler/split và source dataset được đối soát. `scaler_sha256` là
  optional trong schema để tương thích fixture cũ nhưng bắt buộc trong artifact
  LSTM thực tế.
- Bằng chứng M11a (2026-09-30): iot_backend `.venv` `tests/test_model.py
  tests/test_inference.py tests/test_integration.py` = 23 passed; full suite =
  214 passed, 8 skipped, 2 dependency warnings; AI environment TF 2.20.0 /
  Keras 3.15.1 chạy `tests/test_lstm_train.py` = 13 passed, 1 skipped; smoke
  độc lập = 36/36, gồm năm artifact, hash, shape, train-only scaler, finite
  metrics, tamper/missing-file rejection. Artifact chỉ nằm dưới `/tmp/opencode`.
- M11a review không có blocker/major. Note tại M11a về zero-ratio/metric
  config/metrics integrity/import iot_backend đã xử lý ở M12b (2026-10-02): ratios
  tất cả >0, metric chỉ [mae], hash metrics mới + finite/version/count loader,
  timestamp UTC parse thuần stdlib. Metrics dùng checkpoint phát hành, không
  dùng last-epoch weights. Manifest metrics hash optional cho artifact cũ.
- M12a đã khóa các hàm metric thuần để evaluator dùng chung: `rmse_per_horizon`
  và `regression_metrics` kiểm tra ma trận n×H, reject rỗng/ragged/nonfinite;
  `regression_metrics_grouped` không pool hoặc drop group; `event_metrics_extended`
  thêm F1, false-warning rate, miss rate và lead time milliseconds. Extended
  output không phát `inf` (zero-span false alarm trả `None` + reason), còn
  `event_metrics` legacy giữ nguyên kể cả nhánh `inf` để bảo toàn M5. M12a
  chỉ bổ sung primitives; M12b sau đó wiring API/CLI riêng. Evidence M12a: targeted 55
  pass, full 236 pass/8 skip, smoke độc lập 36/36, reviewer không có
  blocker/major. Ba minor test gaps (legacy inf, JSON zero-span, empty-event
  group) phải được xử lý hoặc pin rõ trong M12b.

## Môi trường đã xác minh (2026-09-21)

- Python 3.12.3; `.venv` đã tạo với: paho-mqtt 2.1.0, fastapi 0.141.1, uvicorn 0.53.0, pydantic 2.13.5, jsonschema 4.26.0, PyYAML 6.0.3, scikit-learn 1.9.1, pandas 3.0.6, numpy 2.5.3, joblib 1.6.0, pytest 9.1.1, python-dotenv 1.2.3, httpx 0.28.1, scipy 1.18.1.
- Không có ESP-IDF/xtensa toolchain và không có mosquitto binary trên máy này → firmware chỉ review được (không build); test iot_backend không phụ thuộc broker thật (inject trực tiếp qua `IngestionService.handle_message`); broker qua docker `eclipse-mosquitto` khi chạy thật (docker 29.1.3 có sẵn).
- Người dùng có group dialout (flash serial OK).

## Giới hạn đã cam kết

- Demo hộp đen tham chiếu SSM, tham số robot giả định phần mềm — không tuyên bố đạt ISO.
- Không ghi Blockchain tức thì trong TV3/TV4 (đó là TV1–TV2), nhưng bảo toàn timestamp và thứ tự đo.
- Dữ liệu synthetic phải ghi nhãn `synthetic`, không trình bày như kết quả thực nghiệm.
  Firmware M4j build PASS IDF6.0.1 cho ESP32/S3; người dùng đã cung cấp log
  HRC chạy thật nhưng bản sửa mới chưa nạp bởi Codex, đo/mạng/soak trên bo
  chưa nghiệm thu — giữ rõ trạng thái trong README/docs.

## Bài học quy trình & kỹ thuật M4 (2026-09-22)

- **Task cho glm-5.3-flash phải nhỏ**: task firmware nguyên khối (~10 file C) trả kết quả rỗng 3 lần liên tiếp dù provider sống (diagnostic PONG). Tách 4 giai đoạn (khung → driver → mạng → payload/integration, mỗi task ~2–3 file) thì chạy ngay và chất lượng tốt. M5+ chia task theo nhóm file ≤ ~500 dòng code sinh ra.
- **Lớp review độc lập bắt được bug orchestrator bỏ sót**: reviewer đối chiếu source esp-mqtt pin commit của IDF v5.5 phát hiện `esp_mqtt_client_register_event` (số ít, KHÔNG phải số nhiều) — orchestrator đọc 100% code vẫn kiểm sai. Giữ pattern review độc lập sau mỗi mốc lớn.
- **ESP-IDF v5.5 API đã xác minh** (qua review đối chiếu source): `esp_mqtt_client_register_event`; `.outbox.limit` tính bằng bytes (KHÔNG có `.task.outbox_limit`); `esp_mqtt_client_enqueue(..., store=true)` non-blocking cho telemetry chu kỳ cao; `CONFIG_LWIP_SNTP_MAX_SERVERS` default 1 (muốn NTP dự phòng phải set trong sdkconfig.defaults).
- **RMT RX (driver HC-SR04)**: khi line không có cạnh nào sau TRIG, phiên nhận không tự kết thúc → receiver kẹt trạng thái RUN → phải `rmt_disable` + `rmt_enable` reset. Idle threshold cắt pulse đang giữ mức; giá trị pulse bị cắt là clamp, không dùng làm echo_us.
- **TensorFlow environment (2026-09-30)**: P0 thử trong environment tạm ngoài
  repo với `tensorflow==2.20.0`; import `tf 2.20.0 | keras 3.15.1 | numpy 2.5.3`,
  model LSTM baseline fit 2 epochs, predict `(4,5)`, save/load `.keras` sai
  khác tối đa 0.0. Không thay đổi requirements iot_backend; tái lập bằng
  `python3 -m venv <ai-env> && <ai-env>/bin/pip install tensorflow==2.20.0`.
- **JSON Schema date-time (2026-09-30)**: `jsonschema.FormatChecker()` trong
  `.venv` không đăng ký `date-time` vì thiếu RFC3339 validator; schema
  `format` hiện chỉ là annotation ở môi trường này. Khi M11/M13 validate
  manifest/assessment ở runtime, phải dùng checker có đăng ký hoặc dependency
  đã kiểm chứng; không tuyên bố format timestamp đã được enforce chỉ vì
  `check_schema` pass.
- **Ngữ nghĩa error_code firmware (chốt sau M4e)**: `ECHO_STUCK_HIGH` = ECHO cao TRƯỚC khi đo (precondition); `ECHO_TIMEOUT` = echo có cạnh lên nhưng không xuống trong timeout (echo_us=null ở driver — gồm xung "không có vật" ~38ms; ở mức telemetry, echo hoàn tất 26.2–30ms → distance > 4500 → ECHO_TIMEOUT giữ echo_us thật); `ECHO_STUCK_LOW` = không có cạnh lên nào. Lệnh control lạ/thiếu session_id → KHÔNG ack (ack chỉ dành cho lệnh hợp lệ).

## Quyết định và evidence trực tiếp ngày 2026-10-02

- Người dùng yêu cầu Codex tự triển khai, không dùng subagent. M12b–M16 đã
  triển khai và V1 software self-review/test trực tiếp. Checkpoint OpenCode cũ
  còn next="Chờ M12b1:tester" không phản ánh phiên này; xem PLAN mục 6/8.
  Không mở OpenCode hoặc tạo/chỉnh ID session giả.
- LSTM evaluator/CLI giữ RF API legacy, xác minh artifact config/hash/split.
  RF manifest mới có dataset_sha256; so RF/LSTM phải cùng hash và cả session
  lists train/val/test, artifact RF cũ cần retrain để so sánh. Event metric
  không nối device/boot/run/gap. Metadata target_type/speed_level được giữ.
- Runtime gọi shared feature/scaler, raw tensor (10,2) và inverse output về
  mét trước threshold. `predicted_violation_ms` là horizon đầu <= Sp; Sp
  không xác định → null/early_warning false. TF/artifact/inference lỗi chỉ
  MODEL_UNAVAILABLE, safety vẫn sống. RF cũng không round trước threshold.
- LSTM reset khi boot/session thay đổi (kể cả quay về session=None), error/
  gap/jitter; catch-up prime lịch sử processed trước pending và trước khi
  MQTT listener start. Replay dùng SessionManager boundary của DB.
- EventDetector state lưu SQLite theo device/boot/session/processor. Debounce
  2/3 trên CLEAR+OK+not-stale; giữ first warning assessment và confirmed_ref,
  rearm sau 3 mẫu không warning. VIOLATION/UNKNOWN trigger ngay khi vào trạng
  thái trong run. UUID5 event ID cho idempotency, processed/event/state commit
  atomic. Late assessment không rewind detector.
- Event API `/events` và `/events/{id}` cùng schema event_package_v1. Raw chỉ
  cùng scope từ processed version; bracket mẫu gần nhất ngoài biên khi jitter
  để phủ ít nhất pre2s/post1s khi có dữ liệu. `coverage` báo gap/incomplete;
  package ready khi post window đã qua hoặc session đóng, ready không đồng
  nghĩa complete. Không canonicalize/hash thay TV2.
- Calibration CSV yêu cầu nguồn real/synthetic và context, bias/MAE/sample
  sigma, proposal `max(abs(bias)+k*sigma,max_abs_error)/1000`, max giữa nhóm
  và aggregate. Đây là policy thực nghiệm, không chứng nhận ISO. Không tự
  thay configs/safety.yaml; source hỗn hợp bị từ chối.
- Simulator có telemetry/control/ack QoS1; wire payload giữ schema 11 field,
  recording JSONL có synthetic/scenario/session_label, boot synthetic_*. Tool
  export_dataset lấy closed sessions, thêm metadata cho train và đòi khai
  source; không gắn real cho boot synthetic_/sim_.
- Full test cuối: iot_backend **310 pass/11 skip**, AI **321 pass/0 skip**. Live
  Mosquitto riêng: 70 telemetry, session ack boundary6..55 (50 sample), LSTM
  OK31, safety violation11/unknown2. Replay70→0, 3 package raw complete.
- Synthetic smoke 3 epochs LSTM MAE mean0.097709m, CV0.040475m, RF0.056047m:
  LSTM thua baseline, không chốt chất lượng model. Vẫn cần board flash/chạy đo,
  calibration/session real, model evaluation real và reconnect/soak. Artifact
  test ở /tmp, không commit/đưa model smoke vào runtime production mặc định.

## Firmware build M4f — 2026-10-02

- Máy hiện có ESP-IDF **6.0.1** tại `/home/lucas/.espressif/v6.0.1/esp-idf`,
  environment Python tại `/home/lucas/.espressif/tools/python/v6.0.1/venv`,
  Xtensa GCC 15.2.0. Các ghi chú "không toolchain" ở mốc trước là lịch sử.
- `esptool --port /dev/ttyACM0 chip-id` nhận diện board **ESP32-S3** revision
  v0.2; default project target là `esp32s3`, flash type VS Code **UART** cho
  USB serial bridge này. ESP32 classic phải chọn `set-target esp32` riêng.
- IDF >= 6.0 dùng `main/idf_component.yml` để tải `espressif/mqtt`; lock hiện
  pin 1.0.0/hash và target esp32s3. IDF 5.5 giữ MQTT bundled qua nhánh CMake;
  chưa build lại IDF 5.5. Driver GPIO/RMT, lwip/esp_hw_support khai báo trực
  tiếp, không dựa vào public dependencies của legacy `driver`.
- Đã sửa buffer timestamp JSON thành `HRC_MEASURED_AT_LEN + 2`, không tắt
  cảnh báo truncation. `idf.py build` PASS: app 915104 bytes, app/bootloader
  size checks PASS; evidence `/tmp/blockchain-firmware-check/idf-build.log`.
- Chưa flash firmware HRC. Wi-Fi SSID/password vẫn rỗng; cần điền qua
  `menuconfig`, cùng broker URI. `set-target` tạo lại sdkconfig nên chạy
  trước khi cấu hình mạng; sdkconfig.old cũng phải ignore vì có thể có secret.
- Compile/link không chứng minh phần điện Echo, đo HC-SR04, F2/F3, SNTP,
  reconnect/soak trên board. Các gate này tiếp tục mở; không giả lập checkpoint
  OpenCode hoặc thay đổi cấu hình global.


## Serial Wi-Fi và Docker M4g/M17 — 2026-10-02

- Wi-Fi provisioning qua serial list số/SSID/RSSI/kênh/security, join N với
  masked password, hidden SSID và broker mqtt://HOST:PORT. Adapter nhận UART
  và USB Serial/JTAG (primary hoặc secondary của S3); USB CDC chưa hỗ trợ.
  Namespace NVS `hrc_net` ưu tiên Kconfig. Wi-Fi commit sau GOT_IP, failed
  join restore previous credentials, storage SDK đặt RAM. Đổi Kconfig không
  ghi đè NVS đã provision; dùng serial để đổi mạng/broker.
- Reconnect Wi-Fi backoff1..30s; MQTT start sau IP, timeout3s/retry10s.
  Outbox10240 byte vẫn hữu hạn/không durable; khi đầy/quá hạn có mất mẫu.
  Sensor/enqueue fault log tổng hợp5s với transition/recovery, status có bộ
  đếm invalid/drop. ECHO thấp không tự thành dữ liệu valid sau resetreceiver.
- Network/serial init trước sensor để lỗi GPIO không khóa provisioning.
  Native34 case PASS actual C với mocked I/O; full backend344 pass/11skip;
  build S3 947680B và ESP32 935776B PASS, IDF6.0.1. Native mocks không thay
  thế radio/RMT/điện Echo bất đồng bộ trên phần cứng.
- Bo mới ở ttyUSB0 là ESP32-D0WD-V3 rev3.1/CP2102; bo ttyACM0 S3rev0.2 là
  lần trước. Giữ config S3 trong repo, buildESP32 copy riêng tại /tmp.
  Chọn chip đúng bằng esptool/set-target trước flash; chưa nạp bản sửa.
- Compose default ports1885/8001 để tránh service dự án khác. MQTT/iot_backend
  đang chạyhealthy; containers RF/AI pipcheckPASS, AI artifact integration
  58pass. AI UID/GID theo host, configs/artifacts iot_backend readonly; không
  tự dùng artifact synthetic làm model mặc định.
- Smoke Docker: 50raw đầy đủ, device_ack START/STOP, persistenceSQLite và
  retainedMQTT qua restart; broker-onlyrestart iot_backend tự reconnectPASS.
  Status /health mqtt=true chỉ là listener enabled. Simulator device_id
  phải khớp backendDEVICE_ID khi kiểm tra session ack.
- M18 đã xử lý race START/STOP: paho callback commit raw trước, worker compute
  tuần tự; `assign()` chờ boundary gate trong lúc ACK và stop fallback tính cả
  raw pending. Regression test xác nhận mẫu qua START/STOP có cùng
  `session_id` trong assessment và `session_samples`.
- TestDB synthetic đã backup trong backend_data thành
  synthetic-smoke-20261002.db; active hrc.db mới rỗng cho thu thật. Evidence
  /tmp/blockchain-firmware-check/docker-{smoke,persistence}.json và smoke.db.
- Host LAN hiện192.168.100.3, broker firmware1885. Nếu đổi mạng phải cập nhật
  IP qua serial. Cần flash/đo Echo, SNTP/radio/reconnect/soak và dataset real.
  Không có commit, không thay config global/state.


## Wi-Fi macro/mảng hai chiều M4h — 2026-10-02

- Người dùng chốt danh sách Wi-Fi bằng macro/mảng hai chiều, thay cách nhập
  serial M4g. Điền `firmware/esp32/main/network_config.local.h` (ignored,
  không ghi đè file có sẵn); mẫu `network_config.example.h` có thể commit.
  `HRC_WIFI_NETWORKS` khởi tạo `static const char *const s_known_wifi[][2]`,
  mỗi dòng `{SSID, password}`, 1–16 dòng, SSID rỗng bỏ qua.
- Macro mode `HRC_WIFI_LIST_MODE=1` không nạp Wi-Fi/broker từ NVS cũ và không
  khởi động serial setup. `HRC_MQTT_URI` lấy broker IP LAN/cổng host Docker
  (template tại lần kiểm tra: `mqtt://192.168.100.3:1885`). Đổi danh sách/
  password/broker cần build/flash lại; không cần erase NVS. Không có local
  header hoặc mode 0 vẫn dùng Kconfig/NVS/serial legacy.
- Quét toàn bộ AP records, đối chiếu SSID và security được hỗ trợ; ưu tiên
  RSSI mạnh nhất rồi dòng trước nếu bằng nhau. Mỗi candidate tối đa 15 s
  lấy IP; disconnect lỗi đánh thức sớm, thử candidate tiếp theo. SSID trùng
  cho phép password khác. Danh sách trống/sai không lặp kết nối vô hạn.
- Scan/select chạy trong task, không block event loop/timer. Mất mạng/hết
  candidate → rescan với chờ 1/2/4/8/16/30 s, chỉ GOT_IP reset backoff.
  Scan lỗi bỏ kết quả một phần, clear SDK AP list; không log mật khẩu.
- Native test mới 14 case (selection/fallback/duplicate/security/reconnect/
  scan failure/blank/invalid/32-byte SSID/>20 AP); tổng native 48 pass.
  Full iot_backend **358 passed/11 skipped**, 2 dependency warnings. Actual C
  strict compiler và stub FreeRTOS enforce include order; fixture tách khỏi
  local credentials. Wi-Fi/NVS mock không chứng minh radio bất đồng bộ.
- Build IDF6.0.1 PASS S3 **949680B**, ESP32 **937808B**, size checks PASS,
  không compiler warnings/errors. Evidence trong `/tmp/blockchain-firmware-check/`:
  `wifi-list-{esp32s3,esp32}-build.log` và `wifi-list-pytest.log`. ESP32
  dùng checkout tạm, repo giữ sdkconfig S3. Tên serial port có thể đổi;
  chọn chip/cổng thực tế trước flash, không suy chip từ ttyACM/ttyUSB.
- MQTT/iot_backend Docker vẫn healthy. Chưa flash bản M4h; cần thử scan/IP/MQTT,
  fallback/reconnect, điện Echo, SNTP và soak trên bo. Không commit hoặc
  sửa global OpenCode/state. Frontier real calibration/dataset/model vẫn mở;
  race session START đã được khóa ở M18.


## Log khoảng cách và MQTT recovery M4i — 2026-10-02

- Người dùng báo outbox 10249/limit10240, write timeout3s, enqueue loss và
  subscribe thất bại sau reconnect; yêu cầu distance log + fix MQTT. Đã tạo
  repro red trước sửa: task đo gọi SDK enqueue (có MQTT mutex), và SDK limit
  payload-only có thể vượt khi thêm packet overhead. Xác minh source MQTT1.0.0;
  nguyên nhân vật lý socket timeout trên radio chưa xác định.
- Producer telemetry/ack giờ copy vào mailbox RAM zero-wait, lần lượt 16/4
  message; task `mqtt_tx` riêng gọi SDK, ưu tiên subscribe và ack, mỗi vòng
  tối đa4 message/100ms. Topic/payload đã copy trước khi producer trả; giữ
  message khi SDK lỗi. ESP_OK là accepted trong RAM, không phải broker ACK.
- Outbox32KiB, telemetry budget30KiB, chừa2KiB control/ack và256B subscribe
  ngay cả khi ACK queue có backlog. Tính packet overhead và chờ outbox xuống
  nửa budget trước resume. MQTT/SUBACK/ACK counters và cached outbox hỗ trợ
  chẩn đoán mà task đo không phải lấy mutex SDK.
- Subscribe retry1s, SUBACK timeout15s; chỉ QoS1 SUBACK hợp lệ đặt control
  ready cho telemetry, reset khi reconnect. MQTT callback không làm network
  write. PUBACK không tiến triển15s khi publish đang chờ → disconnect cho
  SDK tự reconnect; ACK tiến triển không bị cắt. Keepalive20s/reconnect5s/
  retransmit5s/network timeout10s. GOT_IP fast path tránh chờ TX mutex khi
  MQTT client đã start.
- Distance log mặc định mỗi1000ms, distance_mm/echo_us/valid/error/sequence,
  hoạt động kể cả mailbox đầy. Kconfig HRC_DISTANCE_LOG_PERIOD_MS:100 để xem
  từng mẫu @100ms,0 để tắt; schema và invalid→UNKNOWN không đổi.
- Native65 case PASS (13 MQTT mới +4 telemetry), full **375 passed/11 skip**,
  2 dependency warnings. IDF6.0.1 build ESP32-S3 **951840B**, ESP32 **939936B**
  PASS, size checks và strict compiler PASS. Evidence tại
  `/tmp/blockchain-firmware-check/mqtt-recovery-{pytest,esp32s3-build,esp32-build}.log`.
- Broker host probe20/20 PUBACK, max6.63ms; nhận23 real telemetry hợp lệ từ
  firmware đang chạy. Probe dùng topic diagnostics riêng, không pollute DB.
  Docker vẫn healthy; đây là health hiện tại, chưa chứng minh bản M4i/radio.
- Buffer RAM/SDK hữu hạn, không durable: outage lâu/full mailbox có drop,
  SDK expiry có thể xóa message. Codex chưa flash bản sửa; cần board smoke/
  reconnect/soak và real calibration/dataset. Không commit/state/global edits.


## Sensor log config M4j — 2026-10-03

- Runtime log có Wi-Fi IP và `control ready`, HC-SR04 init `ready`, nhưng
  không có `telemetry`/distance. Root cause đã xác minh ở build: `sdkconfig`
  cũ thiếu `CONFIG_HRC_DISTANCE_LOG_PERIOD_MS`; `#if CONFIG... > 0` trong
  telemetry.c coi thiếu macro là 0, compile-out toàn bộ log. Không suy diễn
  từ đó rằng RMT/Echo hỏng.
- Thêm option `CONFIG_HRC_DISTANCE_LOG_PERIOD_MS=1000` vào defaults và
  sdkconfig hiện tại; source fallback 1000 ở telemetry/app_main để config cũ
  không tắt im lặng. Người dùng vẫn đặt 0 qua Kconfig để tắt. App in startup
  `telemetry task started; distance log interval=...`; mỗi giây in distance,
  echo, valid/error, sequence.
- Build IDF6.0.1 S3 `0xe8740` (app còn9%) và ESP32 `0xe58a0` (còn10%) pass,
  không diagnostics; full test **375 pass/11 skip**, 2 warnings. Chưa flash
  bản M4j. Sau flash nếu startup line có nhưng distance không ra, điều tra
  tiếp RMT callback/HC-SR04 wiring; nếu log có `ECHO_STUCK_LOW`, kiểm tra
  GPIO4/5, GND chung và Echo divider.


## M18 — session boundary và dữ liệu AI (2026-10-03)

- `MqttListener` không chạy safety/AI/event trên paho network thread.
  `accept_message()` validate + insert raw trước khi callback trả;
  `process_sample()` chạy ở worker tuần tự. Raw pending vẫn được `catch_up()`
  sau restart.
- `SessionManager` gate START/STOP khi chờ ACK. Mẫu đang bay không chạy qua
  `assign()` trước khi row session/boundary được ghi; stop fallback nhìn cả raw
  đã commit. Đây là contract cần giữ trước khi thu dataset nghiệm thu.
- Verification ngày 2026-10-03: `.venv/bin/pytest tests/ -q` **379 passed,
  11 skipped**, 2 dependency warnings; runtime Docker snapshot **67.930 raw,
  67.930 processed, 7 boot, 0 session**.
- AI image sau rebuild source M18: nhóm test LSTM train/inference/evaluation **58 passed**;
  đây chỉ là kiểm chứng pipeline, không sinh artifact production.
- Không dùng 67.930 raw hiện tại để train: chưa có session/scenario/target label
  và có sequence gaps do reboot/network. Chưa có artifact model trong
  `ai_model/artifacts/`; `MODEL_UNAVAILABLE` là trạng thái đúng, safety độc lập.


## M19 — runner thu dataset qua START/STOP (2026-10-08)

- `scripts.collect_dataset` chạy trên host, dùng HTTP API iot_backend Docker
  mặc định8001. Backend là tiến trình duy nhất ghi SQLite và publish control.
  Người vận hành chọn scenario/target/tốc độ; Enter START/STOP hoặc duration.
  YAML hỗ trợ defaults/repetitions; `configs/collection.example.yaml` có60 lượt.
- START/STOP không bật/tắt phép đo: firmware trả ACK mốc boot/sequence, iot_backend
  gán raw vào session. Runner mặc định yêu cầu ACK START trước khi báo bắt đầu
  động tác; fallback chỉ dùng khi chọn --allow-db-fallback và đánh dấu WARN.
- API hiện chỉ công khai nguồn boundary START. Runner ghi nguồn STOP là
  not_exposed_by_api. Không dùng boundary_source để suy diễn ACK STOP.
- Output mới mỗi lần: raw và labelled JSONL từng lượt, dataset tổng, manifest
  atomic/SHA-256. Dòng labelled giữ payload gốc + session_label/scenario/
  target_type/speed_level/synthetic=false. Speed, ghi chú, duration và trạng
  thái request giữ trong manifest; chưa có cột speed riêng trong sessions DB.
- Chờ đủ sequence sau STOP ACK tối đa8s, kiểm tra nhịp theo configs/model.yaml.
  Raw invalid giữ lại, WARN ghi rõ; mẫu ngoài boundary/schema sai/boot synthetic
  không được gắn nhãn real. Dataset tổng chứa cả PASS/WARN, cần xem manifest
  để chọn các lượt phù hợp trước train. Tốc độ là nhãn người vận hành khai báo.
- Ghi spec/trạng thái trước POST, không replay control khi response timeout.
  Manifest needs_reconciliation lưu ID cần kiểm tra bằng GET. Ctrl+C trong
  thao tác đóng/xuất lượt đang sở hữu và ghi interrupted. Giữ thư mục đã có
  dữ liệu; session ID <=63 ký tự ASCII theo giới hạn firmware.
- Hardware smoke 2026-10-08 cho thấy ACK START thực tế có thể đến sau 2 giây
  dù lệnh control đã tới broker và telemetry vẫn chạy; timeout iot_backend 2 giây
  gây `db_fallback` giả trong manifest. Mặc định `HRC_ACK_TIMEOUT_S` hiện là
  8 giây trong Compose/settings/iot_backend docs; runtime đã được recreate theo giá
  trị này. Không dùng `--allow-db-fallback` cho dữ liệu train.
- Verification:30 collector tests PASS; full409 passed/11 skipped,2 warnings.
  Integration qua FastAPI/SessionManager/MqttControlTransport thật, device ACK
  giả lập và telemetry tới sau STOP. Read-only host API smoke PASS, export85
  mẫu lịch sử. Hardware smoke sau đó đã chạy một lượt real `approach/person`
  với 266 mẫu `quality=PASS`; chưa train/promote model production.


## M4k — sửa cơ chế gửi gây ACK backlog (2026-10-08)

- Timeout8s không đủ để khắc phục lỗi thu lượt kế tiếp. Probe broker thật:
  START broker PUBACK1ms/device ACK10.686s, STOP42ms/5.2s; bo vẫn báo
  wifi=1/mqtt=1/control=1 và outbox/mailbox đầy, mất mẫu. Không suy diễn
  connected đồng nghĩa gửi telemetry/control kịp; chưa quy kết nguyên nhân
  radio hoặc reconnect khi thiếu bằng chứng.
- Ưu tiên ACK ở mailbox không tạo ưu tiên trong FIFO esp-mqtt1.0.0. Worker
  hiện dùng esp_mqtt_client_publish cho telemetry/ACK để gửi ngay, giữ
  QoS1/outbox retransmit; producer vẫn copy zero-wait, sampler không chờ SDK.
  Retainfalse, nhịp100ms, schema và boot/sequence session không đổi.
- Regression native RED trước sửa/GREEN sau sửa: ACK vượt backlog80 packet,
  6 lượt30s/10Hz, 2106 telemetry/12 ACK. Menu collector tiếp tục khi đã xác
  nhận owned session closed; POST timeout vẫn needs_reconciliation và dừng.
- Full415 passed/11 skipped/2 warnings. IDF6.0.1 build S3/ESP32 PASS;
  ESP32 configure cảnh báo ROM gdbinit khi thiếu ESP_ROM_ELF_DIR, compiler
  không warning/error. Source/tests/build đã xác minh; nghiệm thu radio/device
  sau flash còn mở. Monitor đang giữ cổng ACM0; một lượt PASS
  trước đây không chứng minh đã xử lý lỗi nhiều lượt liên tiếp.


## Migration cấu trúc repo — 2026-10-08

- Đổi package Python `backend` thành `iot_backend`, `iot_code` thành `firmware`.
- Hợp nhất source repo clone vào `blockchain_api`, `smart_contracts`, `dashboard`; docs tại `docs/blockchain`.
- Repo clone cũ và cache firmware được chuyển ra `/tmp/blockchain-layout-migration-20261008`; Git cha giữ nguyên.
- Compose service/target `iot_backend`; volume SQLite giữ tên vật lý `blockchain-hrc_backend_data`.
- Kiểm tra: Python 415 passed/11 skipped; Node 46 passed; Hardhat compile PASS, 49 passed; Compose config PASS.
- Hai pipeline IoT và blockchain chưa tích hợp tự động; không commit/push trong migration.

## Dataset real / train và báo cáo offline — 2026-10-09

- Người dùng chốt dùng số đo đã thu, giữ các session stationary/stop/
  oscillation gần cảm biến và invalid; scenario mô tả động tác, không đồng
  nghĩa nhãn CLEAR/VIOLATION. Bộ chuẩn data/processed/real-all-20261009.jsonl
  gồm8062rows/22session/7716windows, SHA256
  6680a9e34c5daa1d3066c7d7431f1e31c2fe07f2d498354fb1b51ead5ba8edf4.
- Không thay source measurements hoặc bỏ invalid; script merge kiểm schema,
  nguồnreal, sample key, timestamp/nội dung trùng và session metadata. Nhãn
  speed từ JSONL từng dòng là nguồn dùng trong bộ chuẩn; manifest cũ bị gán
  medium cho cả slow/fast, có5mismatch, ghi audit để không tiếp tục lan lỗi.
- Seed42 là kết quả chính: 16/3/3session, test1026windows; LSTM MAE14.9547mm,
  RMSE27.7978mm (mean5horizons). MAE persistence13.9961mm thấp hơn LSTM;
  không tuyên bố LSTM thắng mọi baseline. Test không crossing nên F1N/A.
- Seed18 đã train độc lập thêm để có event coverage, **exploratory** vì chọn
  split sau xem dữ liệu. Test1172windows/3crossing, LSTM MAE18.5299mm,
  RMSE40.4944mm; LSTM/RF F1offline1.0 với3TP/0FP/lead~100ms. Không pool
  hoặc bỏ kết quả seed42; không dùng seed18 như đánh giá confirmatory.
- F1evaluator là warning episode trước debounce. Chạy EventDetector thật
  offline trên predictions cho seed18: LSTM/RF3warning points nhưng0event
  PREDICTED_WARNING xác nhận2/3. Ba crossing chỉ có1mẫu CLEAR liên tiếp.
  Giữ phân biệt F1offline và event runtime trong mọi trích dẫn kết quả.
- Bundle data/results/real-20261009/README.md có metrics/CSV/predictions,
  PNG300dpi/SVG/PDF và model snapshots; ZIP cạnh bundle. Hash và metric
  recompute PASS; merger6tests, TFartifact/evaluator42tests PASS. Chưa
  deploy artifact runtime. PLAN R1 ghi evidence/giới hạn/tái lập.

## LSTM runtime được chọn — 2026-10-09

- Người dùng chọn LSTM làm model runtime và yêu cầu bỏ Random Forest. Runtime
  Docker mặc định dùng target `ai`, `MODEL_TYPE=lstm`, artifact
  `ai_model/artifacts/lstm-real-20261009`. `.env.example`, `compose.env.example`,
  Compose, README và docs đã đồng bộ. Safety không đổi; model lỗi trả
  `MODEL_UNAVAILABLE`, không fallback RF.
- Xóa artifact RF real `rf-real-20261009` và `rf-real-event-20261009`. Giữ RF
  trainer/adapter/tests và kết quả RF đã ghi trong báo cáo để có thể giải thích
  so sánh cũ; RF không còn là runtime option được khuyến nghị.
- Runtime Docker được xác minh 2026-10-09: `/health` trả
  `status=ok,mqtt=true,model_type=lstm,model_available=true`; predictor load
  manifest `lstm-v1`, smoke 10 mẫu đạt status `OK` và 5 horizon. Container cũ
  dừng, container `iot_backend` mới healthy, volume SQLite giữ nguyên.
- Runtime này phục vụ thử nghiệm/báo cáo, không khẳng định an toàn: chưa có
  test độc lập đủ crossing và F1 sau debounce vẫn chưa chứng minh được. Kế hoạch
  dataset tiếp theo nằm trong `docs/data-collection.md`; giữ test set theo
  buổi/ngày độc lập, ground truth khoảng cách độc lập, không chọn split theo F1.


## Quyết định bền vững — LSTM-only và dataset real 2026-10-10

- Chỉ LSTM là model runtime/train/evaluate của pipeline hiện hành. Persistence
  và constant-velocity chỉ dùng làm baseline so sánh offline; không được gọi là
  model triển khai. Random Forest không còn trong CLI/evaluator/runtime và các
  artifact/report RF cũ đã dọn khỏi thư mục active.
- Dataset canonical mới nằm ở `data/processed/real-20261010/`: canonical 12.820
  mẫu/49 session; LSTM dataset 12.361 mẫu/47 session; sensor fault tách riêng.
  Split theo session trước windowing là 33/7/7 session, 7.046/2.740/1.902
  windows, threshold đánh giá `Sp=0.04 m`.
- Kết quả test hiện tại: LSTM MAE 6,46 mm, RMSE 10,68 mm; 82 crossing;
  precision N/A, recall 0, F1 0, miss rate 1 vì không có warning episode. Đây
  là giới hạn cảnh báo sớm hiện tại và phải ghi rõ trong báo cáo.
- Artifact/runtime active là `ai_model/artifacts/lstm-real-20261010`; Compose và
  env mẫu trỏ vào artifact này, safety demo dữ liệu mới dùng `safety-square.yaml`
  với boundary 40 mm. Firmware generated weights/scaler phải được regenerate
  khi artifact thay đổi.
- F1 metric contract: không có event → F1 null; có event nhưng không có predicted
  positive → precision null, recall 0, F1 0, miss rate 1.

## Benchmark synthetic chạy bằng pipeline chuẩn — 2026-10-10

- Bộ synthetic `data/synthetic/square-40mm-20261010/dataset.jsonl` có 400 session,
  96.000 mẫu, Sp 40 mm, hash
  `0823c297f9248cce2dcb49dab682a9c24ce19a6aebda9e84f3838485de67d6d8`; split
  280/60/60 session được giữ nguyên trước windowing.
- Đã train/test lại bằng `configs/model.yaml` và artifact riêng
  `ai_model/artifacts/lstm-benchmark-square40-20261010`. Không dùng artifact này
  cho Compose hoặc firmware runtime real.
- Test 500 ms: MAE 2,56 mm, RMSE 3,24 mm, 51 crossing, F1 offline 0,9375;
  sau detector 2/3 F1 0,7857, precision 1,0, recall 0,6471, miss rate 0,3529.
- Synthetic được xử lý qua cùng pipeline như dữ liệu thật để kiểm chứng train,
  evaluation và report; provenance `synthetic=true` bắt buộc giữ trong dataset,
  report và hình. Không dùng kết quả này thay cho thực nghiệm ESP32 thật.


## Triển khai local blockchain và edge LSTM — 2026-10-10

- Local Hardhat chain `31337` chạy ở `127.0.0.1:8545`; deployment hiện tại:
  `HRCSafetyLog=0x5FbDB2315678afecb367f032d93F642f64180aa3`,
  `HRCRewardToken=0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512`,
  `HRCSessionRewards=0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0`.
  Các địa chỉ này chỉ có giá trị trong node local hiện tại; restart Hardhat sẽ cần deploy lại.
- Contract policy đã được kiểm chứng live: stake 2 HRC, reward 3 HRC tối đa một lần/ví/UTC day,
  penalty 2 HRC cho `VIOLATION_CONFIRMED`, trả stake và không đổi số dư cho `SENSOR_FAULT`/
  `INCOMPLETE`, reward pool 1000 HRC, settle idempotent. Smoke đúng phải dùng beneficiary
  khác penalty treasury; evidence ngày 2026-10-10 cho thấy `20 → 21 HRC`, pool `1000 → 997 HRC`.
- `blockchain_api` đang chạy local trên port 3000; route `POST /api/session-outcome` đã settle
  một session thật trên local chain qua HTTP. `iot_backend` cung cấp policy outcome ở
  `GET /sessions/{session_id}/outcome` và bridge tự động gọi start/settle khi bật cờ
  `HRC_REWARD_AUTO_START`/`HRC_REWARD_AUTO_SETTLE`. Không có background worker tách riêng;
  việc settle xảy ra trong request stop hoặc endpoint retry thủ công.
- Bảng `reward_settlements` là durable outbox: lưu evidence trước khi gọi API, giữ nguyên
  evidence khi retry sau lỗi, và trả response đã lưu khi session đã `SETTLED`, nên không tạo
  giao dịch thứ hai cho cùng session.
- Edge runtime chỉ dùng LSTM real artifact `lstm-real-20261010`, không dùng RF và không dùng
  synthetic benchmark. Firmware generated source có model/scaler hash, native parity 7 pass/1 skip,
  ESP-IDF 6.0.1 build ESP32-S3 pass, threshold 40 mm. Không được gọi là flash/hardware verified
  khi chưa có serial chip-id, log LSTM, HC-SR04 đo thật và MQTT soak.
- Evidence triển khai được lưu ở `data/results/edge-blockchain-20261010/`; ảnh policy ở
  `reward-policy.png` và `reward-policy.svg`, report chính ở `deployment-report.json`.

## V2 runtime/dashboard verification — 2026-10-10

- Compose demo mặc định dùng artifact synthetic benchmark
  `lstm-benchmark-square40-20261010`, `Sp=0.04 m`, và `HRC_SOURCE_MODE=simulator`.
  Source label là cấu hình operator (`simulator|esp32`), không suy luận từ
  `DEVICE_ID` hoặc tên artifact. Mọi response live vẫn giữ
  `threshold_verified_on_hardware=false`.
- Database runtime đã vượt 1,1 triệu raw rows. Index
  `idx_raw_device_received(device_id, received_at DESC, id DESC)` và truy vấn
  live đi từ raw mới nhất; đo sau restart container: `/live/latest` p50/p95
  khoảng 1–2 ms, `/system/health` 1–2 ms, `/live/history?limit=600` 23/56 ms.
- Chrome headless browser smoke đã render desktop 1440×1000 và mobile 390×844:
  SSE/polling hiển thị `SYNTHETIC STREAM · LIVE`, actual distance, Sp và năm
  horizon. Rule `[hidden]` được khóa bằng `display:none !important` để empty
  chart overlay không phủ dữ liệu thật. Đây là browser smoke/responsive evidence,
  chưa phải full scenario E2E CLEAR → WARNING → VIOLATION → CHAIN CONFIRMED.

## Canonical AI release và deployment retry — 2026-10-11

- Quyết định hiện hành: chỉ giữ một release AI canonical cho demo/runtime:
  `ai_model/artifacts/lstm-v1/` và `data/results/lstm-v1/`. Không dùng lại tên
  artifact/report có hậu tố ngày cho runtime. Artifact/report cũ đã được dọn;
  dataset nguồn `data/synthetic/square-40mm-20261010/` và evidence blockchain
  `data/results/edge-blockchain-20261010/` được giữ vì có vai trò khác.
- `lstm-v1` được train lại bằng TensorFlow 2.20.0/Keras 3.15.1, seed 20261010,
  input `(10,2)`, output `(5,)`, horizons 100-500 ms, split session 280/60/60,
  windows 63280/13560/13560, scaler train-only. Model hash:
  `94d017fa94ca25ee22bacba740ac37102bd1537c82b009a61817cb62506b2b52`;
  scaler hash:
  `20281417e61b5847f61413afa12245284ac88e6ae16b0d1c12b01da53e03986d`;
  dataset hash:
  `0823c297f9248cce2dcb49dab682a9c24ce19a6aebda9e84f3838485de67d6d8`.
- Kết quả synthetic benchmark horizon 500 ms: MAE mean 2.56 mm, RMSE mean
  3.24 mm, F1 offline 0.9375; detector 2/3: precision 1.0, recall 0.6471,
  F1 0.7857, miss rate 0.3529, lead time mean 124.2 ms. Luôn gắn nhãn
  synthetic-only; không suy diễn thành hiệu năng HC-SR04 hoặc safety hardware.
- Firmware source đã được regenerate theo hash mới và native parity tests đã
  cập nhật. Hardware flash, serial inference, sensor measurement, calibration,
  reconnect/soak vẫn chưa verified.
- Compose/env/settings/docs mặc định trỏ `lstm-v1`; khi thay source phải
  rebuild image vì service không mount source code. Deployment retry phải dùng
  `docker compose build iot_backend ai` rồi recreate `mqtt` và `iot_backend`,
  kiểm tra health/API và chạy test host + Docker.

- Deployment retry đã hoàn tất ngày 2026-10-11: build image AI/backend PASS;
  Compose recreate với MQTT và iot_backend PASS sau khi healthcheck backend
  tăng `start_period` từ 20 lên 60 giây để bao phủ cold-start TensorFlow.
  `/health` healthy; `/models/active` xác nhận `lstm-v1` và đúng các hash;
  `/live/latest` trả prediction `OK` năm horizon, simulator source và không
  claim hardware verification.
- Regression cuối: host `441 passed/12 skipped`, Docker `380 passed/73 skipped`,
  targeted firmware/runtime `11 passed/1 skipped`; synthetic verifier trong
  image mới PASS. `docker compose config` và `git diff --check` PASS.

## Synthetic V2 release and promotion decision — 2026-10-11

- Full deterministic release is `data/synthetic/square-bench-v2/`, seed
  `20261010`, with 1,200 forecast sessions + 150 fault sessions, 270,000
  samples. Dataset SHA-256 is
  `56fd67c14a155361f37f7e90f5e21b3341e7fc8463d193c4b57043818440c87c`;
  telemetry SHA-256 is
  `de0bfdf4f258811b2b70241948de8a532c25bc2016ca25fac357ade57837c069`;
  ground-truth SHA-256 is
  `e3af08bc6e50941f338d787ac71771e7e60c04f05eed74805aa6b7b5863cf783`.
- `scripts.prepare_synthetic_v2` creates runtime-shaped forecast/fault views;
  it excludes `distance_true_m`, future targets, crossing labels, seed and
  split from runtime rows. Fault sessions never enter model training.
- Candidate config is `configs/model-synthetic-v2.yaml`; candidate artifact is
  `ai_model/artifacts/lstm-v2-synthetic/`. Three seeds use the same frozen
  840/180/180 session split and 156,240/33,480/33,480 windows. The candidate
  status is `REJECTED_SYNTHETIC_GATE`: constant-velocity wins regression and
  the LSTM false-warning rate is too high. Do not replace `lstm-v1` based on
  this release.
- `tests/test_product_e2e.py` is the local product gate for session → telemetry
  → confirmed event → chain retry → reward retry → duplicate readback. The
  safety/event data remains available while asynchronous chain/reward bridges
  are unavailable; retries reuse immutable payload/evidence.
- Release evidence bundle is `release/square-bench-v2-20261011/` (ignored).
  It contains redacted/local manifests, checksums, evaluation evidence and
  runbooks, but no private keys, tokens or full generated dataset.
- The above is software/local evidence only. Hardware flash/measurement/soak,
  public/testnet deployment with funded signer and receipts, and production
  safety certification remain open gates.

## Final local deployment verification — 2026-10-11

- Runtime cuối cùng chạy image AI có TensorFlow 2.20.0/Keras 3.15.1. `/health`
  xác nhận `model_available=true`, MQTT healthy, reward bridge và chain bridge
  đều configured/running khi dùng `compose.env` cùng token truyền qua environment.
- `/models/active` xác nhận active model `lstm-v1`, model SHA-256
  `94d017fa94ca25ee22bacba740ac37102bd1537c82b009a61817cb62506b2b52`, input
  `(10,2)`, output `(5,)`, `training_data=synthetic-only`,
  `simulation_mode=true`, `threshold_verified_on_hardware=false`.
- Hardhat local `31337`, blockchain API `3000`, MQTT/API Docker `1885/8001` cùng
  chạy. Reward/API evidence đã được đưa vào release bundle; public/testnet không
  được suy diễn từ local receipts.
- Final verification: host `444 passed/12 skipped`, blockchain API `53 passed`,
  smart contracts `64 passing`, synthetic audit PASS, compose config PASS,
  release checksums `25/25 OK`.
- Không commit token/private key, `.opencode/state/` hoặc dataset/artifact lớn.
  Hardware, public/testnet và production certification vẫn là open gates.

## Application wallet identity and customer behavior (R9, 2026-10-11)

Application login is separate from blockchain ingest/admin bearer tokens. The
browser signs an EIP-191 challenge with MetaMask; the API stores only a hash of
the app session token and persists wallet/user bindings plus off-chain behavior in
`HRC_CUSTOMER_STORE_PATH`. Customer directory access comes from the explicit
`HRC_APP_ADMIN_WALLETS` allowlist; an owner wallet is not automatically an app
admin unless configured.

Behavior events are idempotent per user and key. Dashboard identity changes must
invalidate in-flight login work and pending behavior must carry the wallet identity
that produced it, so it cannot be attributed to a later wallet. The customer store
uses an atomic snapshot and PID lock with stale-lock recovery. Safety evidence and
reward settlement remain in their existing stores and authorization paths.


## R9 local deployment evidence (2026-10-11)

The wallet/customer implementation passed 58 blockchain API tests and the
unchanged smart contract suite passed 64 tests. Local runtime is currently
available on dashboard `8080`, blockchain API `3000`, and Hardhat chain `31337`
through RPC `8545`; safety and reward deployment manifests match the running
contracts. The application store was cleaned after smoke testing, so no synthetic
customer record is presented as real user data. MetaMask signing still requires a
real browser extension interaction.

## Demo wallet UX decision (R10, 2026-10-11)

The dashboard keeps account selection out of the topbar form. A compact `DEMO
ACCOUNT` trigger opens a small account picker modal for the fixed `admin`,
`user_1`, and `user_2` identities. The wallet workspace appears at the top of Dashboard and in Reports, where a
selected demo user sees wallet address, HRC balance, allowance, approval, session
start, transaction/event receipts, and session history. Navigation remains
Dashboard, Live Monitoring, Safety Events, Reports; admin views reports only.

The reward policy is shown beside the user actions: a session locks 2 HRC; a
valid outcome returns 2 HRC and pays the duration-based reward when the daily
policy allows it; a confirmed violation forfeits the 2 HRC stake; sensor fault
or incomplete returns the stake without reward. Users submit approval and
session start only; the backend operator submits settlement from the recorded
safety outcome.


## R10 verification and recovery behavior (2026-10-11)

- Frontend keeps the selected demo account authoritative during refresh, ignores
  stale account responses, clears expired logins, and disables duplicate writes.
  Pending start references are stored per user before submission and survive
  reload; retry reads the same contract session even after allowance is consumed.
  Post-write refresh waits for any old read and then requests fresh wallet state.
- Demo API serializes writes per user and rejects signing off local chain 31337.
  Stable Hardhat wallet derivation and the durable reference store preserve
  account/session mapping across service recreation. App login tokens are in
  memory and must be renewed after API restart.
- Wallet history is read from ERC-20 Approval and reward contract events, filtered
  to the selected wallet, grouped by hash and scanned in bounded block chunks.
  Settlement remains operator-only; users cannot submit outcome or payout amounts.
- Final suites: blockchain API 71 passed, smart contracts 64 passing. Permanent
  browser verifier `scripts/verify_demo_wallet_ui.mjs` checks actual input/clicks,
  account isolation, read-only admin, refresh, Reports and desktop/mobile. Default
  mode asserts it sent no approval/start requests. The transaction mode already
  verified Approval, exact stake, all fixed-policy outcomes, daily cap, retries,
  event history and automatic result refresh on the running Hardhat chain.
- Browser/receipt evidence is `data/results/demo-wallet-ui-20261011/`. Outcome
  evidence in `settlement-1..5.json` is explicitly simulated and session-bound;
  it does not establish physical robot integration. Dashboard-created session
  IDs still need matching safety/simulation evidence submitted by the operator.
  Preserve existing chain history; do not reset or silently settle active sessions.


## Account-bound sensor sessions and complete runtime build (R11, 2026-10-11)

- Local dashboard account picker contains only `admin`, `user_1`, `user_2`.
  Admin reads both users; Approve/Start/End require the customer role and session
  ownership. Default `HRC_APP_AUTH_MODE=demo` also rejects legacy arbitrary wallet
  registration before creating customer data; legacy EIP-191 requires explicit
  mode `wallet`. Two Hardhat wallet derivations stay fixed after API restart.
- Node coordinates one physical device: reserve session, open sensor using the
  same ID, stake 2 HRC, End the sensor, read its outcome, freeze evidence and
  timestamp, then operator settlement. User input contains references only.
  Contract determines refund/reward/penalty/daily cap. Device readiness cache is
  invalidated after lifecycle writes so End does not leave a stale busy device.
- Store persists workflow, immutable evidence and signed stake/settlement bytes
  before broadcast. Signer writes across safety/reward services are serialized;
  recovery reuses the same signature, nonce and hash. Background scans coalesce.
  Unlinked historical stakes use INCOMPLETE with explicit recovery provenance.
  Pending references and account generation checks prevent duplicate or stale
  browser actions. The visible ledger reads Approval/Transfer/reward events.
- Outcome API must not call global history catch-up: on the runtime database
  that replays about 1.4 million records and times out. `catch_up_session` only
  computes missing rows within the device/boot/sequence boundary under compute
  lock and rechecks processed status. Global startup recovery remains separate.
- User explicitly requires a complete normal Dockerfile build, including AI
  dependencies. Runtime base remains `python:3.12-slim-bookworm`; dependency
  stages precede source copies. Optional pip wheel-cache build arguments do not
  skip requirements or replace the base with an existing running image. Full
  build/deployment image is `0710e46941e8`; pip check/model/hash/source/volume
  verification passed. IoT/MQTT/dashboard are healthy with canonical `lstm-v1`.
- Final verification: API 93 passed, contracts 73 passing, IoT tests inside full
  image 61 passed. Native browser verification with actual ESP32 and disposable
  in-process Hardhat passed Start/End, refunds, ledger, account roles, mobile,
  reload and resume of the same settled session. The measured session had
  sequence gaps: INCOMPLETE, 20→18→20 HRC, 2 returned and no reward. Simulated
  policy integration tests are distinct from this actual sensor evidence.
- Current original localhost RPC 8545 is unavailable. Its manifests/store are
  preserved and the original valid sensor session remains SETTLEMENT_PENDING;
  the isolated verifier does not settle it. API checks saved deployment and
  confirmed receipt anchors and refuses an empty replacement chain. Restoring
  the original RPC/state is required to continue that history. API persistence
  and Docker SQLite volume persistence do not persist Hardhat's in-memory chain.
- Evidence remains ignored under `data/results/live-wallet-sessions-20261011/`;
  signed bytes are private to mode600 backend store/backups, never frontend or
  public evidence. No firmware/global OpenCode changes, commit or push for R11.


## Clean local demo bootstrap and chain payload boundary (R12, 2026-10-11)

- Run the complete local demo with `./scripts/run_local_demo.sh`; use `--smoke`
  for a self-cleaning Hardhat/API check without Docker. Each run stores its own
  manifests, event archive, demo sessions and logs under ignored
  `.runtime/local-demo/`, so a new in-memory chain cannot reuse old addresses.
- Local deployment JSON is generated evidence and is not tracked. The API reads
  `HRC_SAFETY_DEPLOYMENT` and `HRC_REWARD_DEPLOYMENT`. Network selection uses
  `HRC_BLOCKCHAIN_NETWORK`; generic `NETWORK` is intentionally ignored.
- IoT `PREDICTED_WARNING` is sent to blockchain_api as `state=WARNING` with
  `sourceEventType=PREDICTED_WARNING`. Null optional AI fields and a null
  session ID are omitted at the boundary.
- Dashboard reaches blockchain_api through `/api/chain` on its own Nginx origin;
  IoT remains under `/api/iot`.
- Verified locally: Python 446 passed/12 skipped, blockchain API 98 passed,
  contracts 73 passing, clean real-chain smoke passed twice. Docker Compose was
  not executable in the review environment; only YAML/static proxy wiring was
  checked there.

## Dashboard API routing resilience (R13, 2026-10-11)

- The normal Compose dashboard uses same-origin `/api/chain` and `/api/iot`
  Nginx proxies. Upstream 502/504 responses are converted to structured JSON so
  frontend code never tries to parse an Nginx HTML error page.
- The documented Python static-server mode has no reverse proxy. If the default
  `/api/chain` route returns HTML or `CHAIN_API_UNAVAILABLE`, the dashboard
  retries `http://<current-host>:3000`. An explicit `HRC_CHAIN_API_URL` remains
  authoritative and is never replaced by this fallback.
- The local demo is not ready until `/api/chain/api/config` parses as JSON with
  `success=true`. A stale dashboard process or missing blockchain API therefore
  fails at startup with actionable logs instead of opening a broken wallet UI.

## Duration-based HRC reward (R14, 2026-10-11)

- The contract is authoritative for reward amounts. A valid, violation-free
  session earns a 3 HRC base reward at the 30-second validity threshold, with a
  linear duration bonus reaching 100% (6 HRC total) at 60 seconds. Rewards cap
  at 6 HRC after 60 seconds. The existing one-reward-per-beneficiary-per-UTC-day
  limit and reward-pool check still apply.
- Duration is measured on-chain as `block.timestamp - startedAt`; clients and
  the API cannot choose a payout amount. Confirmed violations still forfeit the
  2 HRC stake. Sensor faults and incomplete sessions return the stake and pay no
  reward.
- The dashboard shows the 30s/3 HRC, 45s/4.5 HRC and 60s+/6 HRC scale beside
  wallet actions. API policy summary reads duration and amount limits from the
  deployed contract.
- Verification on 2026-10-11: smart contracts 74 passing and blockchain API
  103 passing. A fresh local Hardhat deployment/API returned the v3 policy; a
  chain-time-advanced 45-second valid session emitted `RewardPaid=4.5 HRC` and
  returned the 2 HRC stake. This was a local contract check, not a physical
  ESP32 session. Dashboard browser verification passed at 1440x1000 and 390x844
  with no horizontal overflow or JavaScript exceptions.

## Dashboard safety-events cleanup (2026-10-11)

- Legacy `/api/events` currently returns an empty safety-event list in the local
  reward demo and duplicated the live safety/session surfaces. The dashboard no
  longer exposes a separate `Safety Events` navigation item, empty event count
  card, duplicated event table, or event-detail modal.
- `Dashboard` keeps account/session actions and a compact refresh/status row;
  `Live Monitoring` is the source for current ESP32 telemetry, threshold and AI
  assessment; `Reports` is the read-only location for reward/session and
  transaction history.
- Frontend no longer requests `/api/events` during Dashboard refresh. Browser
  verification and blockchain API tests were updated for the reduced surface.

## Blockchain re-audit (2026-10-11)

- Wiring: token `0xe7f1…0512`, HRCSessionRewards `0x9fE4…f6e0`; `rewardPool()`
  points at the contract itself, `penaltyTreasury` = `0x7099…79C8`, owner =
  `0xf39F…2266` is a registered operator.
- Invariant `token.balanceOf(rewards) == rewardPoolBalance + escrowedStakeTotal`
  holds before and after settlements; `escrowedStakeTotal` returns to `0`, so no
  stake is left stranded.
- Duration bonus curve on chain: 30s → 3.0 HRC, 45s → 4.5, ≥60s → 6.0 (cap
  100%), matching `rewardPolicy.js`.
- End-to-end smoke (`scripts/smoke_reward_service.js`) against the running local
  stack: valid → `REWARDED` +3; second valid same day → `REWARD_DAILY_LIMIT`
  (stake returned, no reward); violation → `PENALTY` 2; sensor fault →
  `NO_REWARD` (stake returned); duplicate retry → `duplicate: true` with no new
  transaction. Pool 1000 → 997; beneficiary 20 → 23 (20 − 8 staked + 6 returned
  + 3 reward + 2 penalty to the same account acting as treasury).
- `npx hardhat test` → 74 passing; `npm --prefix blockchain_api test` → 103/103.

## P1–P7 wallet-first hardening (2026-10-12)

- **P1 SIWE** behind `HRC_APP_AUTH_MODE=siwe`: `POST /api/auth/challenge` issues
  the EIP-4361 message, `POST /api/auth/wallet` verifies the signature and
  returns an app token plus a reward-session token. Legacy EIP-191 demo login is
  kept as a secondary path.
- **P2** session-ID prefix constraint removed; the store still reads legacy
  prefixed IDs.
- **P3** reward contract refactored onto OpenZeppelin `Ownable2Step`, `Pausable`
  and `ReentrancyGuard`, with a `maxRewardPool` ceiling on `fundRewardPool`.
- **P4** reward policy is now on-chain and owner-tunable: `MIN_REWARD_DURATION`
  (30s), `FULL_BONUS_DURATION` (60s), `MAX_REWARD_BONUS_PERCENT` (100) via
  `setPolicy()`. `rewardAmountForDuration()` scales linearly and caps at +100%.
- **P5** SQLite store selected with `HRC_APP_STORE=sqlite`, plus
  `scripts/migrate_json_to_sqlite.js` for JSON → SQLite migration.
- **P6** admin surface: `GET /api/admin/reward/status`, `GET /api/admin/audit`,
  pool funding, policy/pause toggles and treasury sweep (`sweepExcessToken`,
  `setPenaltyTreasury`), all recorded in the audit log.
- **P7** dashboard is wallet-first: the user connects a wallet and signs SIWE;
  the demo account picker remains as a secondary mode.

### Redeploy + verified behaviour

- Fresh local deployment of `HRCSessionRewards` at
  `0x0DCd1Bf9A1b36cE34237eEaFef220932846BCD82` (token `0xA51c…91C0`); the
  previous contract lacked `paused`/`maxRewardPool`, so P3/P4 were inert until
  this redeploy.
- Live checks against the running stack: reward pool funded to 1050 HRC; a
  45-second valid session paid `RewardPaid = 4.6 HRC` and returned the 2 HRC
  stake; a violation session kept `rewardPaid = 0`, forfeited the 2 HRC stake
  and credited exactly 2 HRC to the penalty treasury.
- Duration curve read from chain: 30s → 3.0, 45s → 4.5, 60s → 6.0, 300s → 6.0
  HRC (cap reached at 60s).
- Wallet-first E2E: SIWE challenge → signed login → `prepare/approve` and
  `prepare/start` return unsigned transactions for the user to sign locally; the
  backend never signs on the user's behalf.

### Bug found and fixed during verification

- `RewardService.getOperatorStatus()` / `getAdminStatus()` did not set
  `success: true`, and the shared `respond()` helper derives the HTTP status from
  that flag, so `/api/admin/reward/status` answered **503** with a perfectly valid
  body. The existing admin tests used a fake service that always set `success`,
  which hid the defect. Both methods now set `success: true` and
  `blockchain_api/tests/rewardServiceStatus.test.js` exercises the real service so
  the regression cannot return.
- Test totals after the change: `npx hardhat test` → 81 passing;
  `npm --prefix blockchain_api test` → 126/126.


## Wallet connection feedback and retry (R18, 2026-10-12)

- The header wallet button already had a working click listener. The verified
  defects were misleading demo copy overwriting the connect label, a raw
  `undefined.request` error when MetaMask was absent, and feedback far from the
  connection action. Do not infer click failure from `element.onclick`: listeners
  registered via `addEventListener` are not reflected there.
- In wallet/siwe mode, the account panel now has a Connect wallet button and live
  status. Missing extension, wrong network, connection cancellation and SIWE
  cancellation stay visible with a retry action. Connected and authenticated
  are separate states; hide the CTA only after authenticated login succeeds.
- The wallet provider is rediscovered on retry if it was absent during page load;
  account/chain/disconnect listeners are registered once for that provider.
  Missing providers update state without rejected/unhandled promises.
- Demo rendering preserves wallet labels. Versioned app/wallet/CSS URLs refresh
  cached browser assets. Demo mode retains its existing fixed account picker.
- Verification: `scripts/verify_wallet_first_ui.mjs` uses native Chromium clicks,
  a disposable test wallet (key stays in Node), real SIWE verification by the
  running local API and reads of the local contract. Seven scenarios pass at
  desktop/mobile sizes, without JavaScript exceptions or horizontal overflow.
  No HRC transfers/session transactions are broadcast by this verifier.

## Local RPC recovery and MetaMask Add Network (R19, 2026-10-12)

- The dashboard connection error was caused by Hardhat RPC being down, not by the
  MetaMask button. Hardhat is now running on `http://127.0.0.1:8545`, chain ID
  `31337`; blockchain API `3000` reads the deployment generated for that same
  chain and the dashboard proxy returns the matching config.
- `dashboard/wallet.js` handles MetaMask error `4902` by requesting
  `wallet_addEthereumChain` for `HRC Local` (`31337`, `Hardhat ETH`, RPC
  `127.0.0.1:8545`) and retries `wallet_switchEthereumChain`.
- `scripts/verify_wallet_first_ui.mjs` now models Add Network acceptance and uses
  `rm` retry options for Chrome profile cleanup. Browser verifier passes 7
  scenarios, including wrong-network Add Network followed by SIWE login.
- A newly connected external wallet is not funded by contract deployment. Do not
  request or store its private key/seed phrase. Before Approve/Start, provide
  local-only ETH for gas and HRC test balance via a controlled faucet or an
  explicit transfer to the user's full public address.

## MetaMask-only app auth (R24, 2026-10-12)

- The supported application login is MetaMask + SIWE only. Demo-account login,
  fixed wallet identities, and demo API routes are removed. Application and
  reward access use the same wallet-derived user identity. Do not ask for an
  external wallet's private key or seed phrase.
- `scripts/run_local_metamask.sh` defaults to dashboard `localhost:8081`, API
  `127.0.0.1:3000`, Hardhat RPC `127.0.0.1:8545`, IoT API `8002`, MQTT `1886`,
  chain `31337`, and SIWE origin `http://localhost:8081`. It refuses occupied
  ports before deployment. Its local test key is generated only for the known
  local Hardhat wallet and saved mode `0600`; never use it on a public chain.
- Session persistence module is `blockchain_api/sessionStore.js`. Existing
  `.runtime` JSON filenames, migration env aliases and SQLite `demo_sessions` /
  `demo_transactions` tables are retained to read prior session/evidence data;
  no runtime data is moved or rewritten by the source rename.
- Current docs/verifier use `verify_wallet_first_ui.mjs`; contract integration
  tests use `WalletLiveSessions.js` with isolated simulated sensor fixtures.
  Browser tests do not prove an installed MetaMask extension signed or broadcast
  a transaction, and fixture tests do not prove real ESP32 behavior.
- Do not restart the existing local stack, deploy, reset chain state or run a
  write-producing smoke test during this task. Keep the user's
  `firmware/esp32/.vscode/settings.json` change out of the commit.

## Wallet-first Start reconciliation (R20, 2026-10-12)

- A wallet-first `POST /api/me/sessions/start` response is an unsigned
  transaction preparation. After MetaMask broadcasts it, the dashboard must
  call `POST /api/me/sessions/:sessionId/confirm-start` with the transaction
  hash. The backend verifies the on-chain beneficiary/session, persists the
  session record, opens the matching IoT session and sets stage `RUNNING`.
- Without this confirmation, the chain can contain `SessionStarted` while the
  JSON/SQLite session store has no record, so the dashboard cannot render
  `End Session`. This was fixed in R20 and tested with the live-session fixture.
- If sensor startup fails after the wallet stake is already confirmed, the
  backend records an explicit incomplete recovery and settles the stake instead
  of leaving an orphaned locked balance.
- Two orphan sessions created before R20 were settled as `INCOMPLETE` with
  explicit recovery evidence; the test wallet returned to `20 HRC` and the
  reward contract escrow returned to zero.


## Wallet-first End execution and session clock (R21, 2026-10-12)

- POST `/api/me/sessions/:sessionId/end` always invokes LiveSessionService.end.
  Approve/Start prepare user signatures; End executes backend STOP/outcome/operator
  settlement. Returning prepareEndSession text from this POST left sessions running.
- Real lifecycle guards apply to wallet-first End: owner isolation, empty body,
  frozen evidence and idempotent retries. GET prepare/end remains descriptive.
- Wallet panel shows HH:MM:SS from sensor started_at/ended_at, ticking each second
  locally, frozen at the closed sensor boundary. Unknown timestamps use only
  reported duration. Sample counts stay backend-derived. Account reads refresh
  every 5 seconds in both SIWE/wallet and demo modes; Start is disabled while a
  session is running/stopping/settling. Removed duplicate duration text.
- Tests: blockchain API 135/135; Chromium verifier 9 scenarios including mobile
  and desktop timer fixtures, no JS errors/overflow. Runtime End on the reported
  wallet session closed the sensor, settled VIOLATION_CONFIRMED, collected 2 HRC
  from escrow, and left available balance 18 HRC / locked 0. Repeat End did not
  create a second settlement; contract token balance = reward pool + escrow.
- API restart preserves chain, manifests and session records, but reward login
  tokens are in-memory; reconnect/sign SIWE after an API restart.

## Reward duration, policy and telemetry coverage (R22a/b, 2026-10-12)

- `HRCSessionRewards.setPolicy()` rejects changes while `escrowedStakeTotal > 0`.
  This preserves the stake/pool accounting invariant without per-session policy
  snapshots. Policy changes remain possible after every stake settles.
- A valid session below `MIN_REWARD_DURATION` returns its stake but receives no
  reward; `previewSettlement()` and `rewardAmountForDuration()` use the same
  minimum. Confirmed violation still follows the penalty outcome.
- Reward eligibility expects 10 Hz telemetry: at least 90% of expected samples
  (minimum 270 for 30 seconds), >=95% processed/raw coverage, 8–12 Hz average
  cadence, monotonic timing span >=90% of wall duration, and no timing gap above
  500 ms. Missing coverage/timing metadata fails closed as INCOMPLETE.
- Evidence: Hardhat 83 passing, Python full suite 449 passed/12 skipped,
  blockchain API 135 passing. These are source-level changes; `HRCSessionRewards`
  is not a proxy, so every deployed contract needs a new deployment before the
  protections apply on-chain. Do not overwrite a running deployment implicitly.

## Wallet start and remaining hardening (R22c–f, 2026-10-12)

- Wallet-first preparation creates a persisted `WALLET_INTENT` with a 120-second
  sensor lease and policy snapshot before unsigned calldata is returned. A
  successful receipt must match configured chain, `from`, reward-contract `to`,
  transaction hash, `startSession(sessionKey)` calldata and `SessionStarted`
  event/session/beneficiary/stake. Pending receipts do not start the sensor; a
  late confirmed stake after lease expiry is frozen as incomplete for recovery.
- Focused lifecycle tests now exercise pending then confirmed receipts, wrong
  chain/sender/recipient/status/calldata/event, competing lease and TTL expiry.
  Focused result: 21 passing.
- Identity consolidation, configured-chain routing and admin/security controls
  have partial source changes and tests only. Do not represent R22d–f as done:
  actual SQLite migration, Sepolia/Amoy deployment/RPC, production reverse-proxy
  TLS/HSTS behavior, browser CSP compatibility and multisig/timelock ownership
  transfer remain unverified or undeployed. This work did not deploy contracts,
  alter a running chain, transfer ownership or configure external TLS.

## Frozen reward end time (R23, 2026-10-12)

- `submitSessionOutcome`, compatibility `settleSession`, and
  `previewSettlement` accept the sensor-close `endedAt` Unix timestamp. The
  contract rejects `endedAt < startedAt` or `endedAt > block.timestamp`, stores
  it on the session, emits it in `SessionSettled`, and computes reward duration
  as `endedAt - startedAt`. `getSessionEndTime` provides a direct read while
  preserving the prior `getSession` output ordering.
- Live Node settlement derives seconds from the ISO-8601 `ended_at` in the
  frozen evidence; the SHA-256 evidence hash includes that same evidence. The
  optional IoT bridge also sends seconds and persists them in its outbox response
  so retries reuse the same value. Sensor-missing recovery is explicitly marked
  with a chain-bounded timestamp and stays `INCOMPLETE`.
- Delayed-settlement regression cases cover 30s→3 HRC at settlement +60s and
  45s→4.5 HRC after delay, invalid bounds, preview/settlement parity and retry
  idempotency. Contract is not a proxy: deployments with the old ABI keep the
  old behavior until redeploy and deployment-manifest update. No chain or
  manifest was changed during R23.

## Safe-to-Earn task rewards V3 (R25, 2026-10-12)

- Keep `HRCSessionRewards` V1 unchanged. Task-bound V2 is a separate
  `HRCTaskRewards` contract selected only by its versioned deployment manifest.
  Local economics are 0.200 HRC stake, 0.002 HRC safe-task reward, 0.010 HRC
  confirmed-violation penalty and 120-second minimum safe duration.
- Task definition hash/policy snapshot is pinned into the assignment and the
  on-chain start event. Completion evidence is owner/operator approved and must
  be stored while the sensor session is still RUNNING, before End freezes the
  evidence document and settlement payload. Retry cannot replace frozen values.
- The local MetaMask launcher keeps V1 as default. `--task-rewards-v2` deploys
  into a fresh run directory; account 0 is local-only SIWE admin/operator and
  account 2 is the funded customer. Their private-key files are mode 0600.
  Never import these local keys on a public network.
- V2 lifecycle integration in `smart_contracts/test/HRCTaskLifecycle.integration.js`
  uses the production wallet calldata builder, task completion service, live
  session coordinator, durable transaction writer, settlement service, and a
  real local `HRCTaskRewards` deployment. It verifies delayed safe settlement
  and immutable `endedAt` /
  evidence hash after retry, plus incomplete, confirmed-violation, sensor-fault,
  and accounting-invariant outcomes. It now also boots the real Python FastAPI
  ingestion/session service on an ephemeral port with its own SQLite database and
  drives it over HTTP through `IotSessionClient`; only MQTT transport, device ACKs
  and wall-clock are deterministic. No ESP32, broker, production endpoint or
  public chain is involved.
- `blockchain_api/taskEvidencePolicy.js` owns the frozen evidence policy
  (10 Hz, 90% sample/raw coverage, 95% processed coverage, 8–12 Hz cadence, 90%
  timing span, 0.5 s max gap, zero sequence gaps). Task snapshots carry it and
  `LiveSessionService` re-derives those checks from the raw session boundary
  instead of trusting IoT `result` flags; a failed check forces `INCOMPLETE` and
  clears `dataValid`/`coverageValid`.
- Wallet session responses preserve task/assignment IDs and policy snapshots.
  Dashboard duration/sample guidance uses that snapshot (V2: 120 seconds and
  1,080 minimum samples at 90% of 10 Hz); V1 retains 30 seconds / 270 samples.
  API/dashboard regression tests cover both response propagation and rendering;
  browser visual/interaction validation remains open.
- Verification after the lifecycle suite and policy-guidance fix: Blockchain API 180/180, Hardhat
  105/105, Python IoT/model 449 passed/12 skipped; JS/shell syntax and
  `git diff --check` pass. The earlier isolated launcher smoke still covers SIWE
  role separation and task create/publish/accept. Real IoT-to-chain recovery/
  chaos, dashboard browser/visual verification, warning/spike telemetry, edge
  parity, HIL, public testnet, TLS, and multisig readiness remain open. No active
  ports, live deployment, hardware, or deployment manifest was changed.

## V2 boundary/recovery/authority hardening (R26, 2026-10-12)

- `LiveSessionService` independently validates every task outcome before freezing
  the settlement payload. A safe result needs completion evidence plus the frozen
  duration/raw/processed/cadence/timing/gap policy. A penalty needs a confirmed
  violation counter; a single spike cannot select `VIOLATION_CONFIRMED`. A sensor
  fault needs unknown evidence or a boot reset. Invalid claims become `INCOMPLETE`.
- V2 wallet start accepts only `sessionRef` and `taskId`; policy snapshots are
  loaded from the server-side assignment, and the assignment wallet/device must
  match the authenticated wallet and configured sensor.
- `POST /api/session-outcome` is V1-only. V2 requires a SIWE operator/admin for
  `POST /api/admin/task-sessions/:sessionId/settle`, with an empty body; the
  route settles the already frozen evidence and cannot accept outcome, beneficiary,
  evidence hash or amount fields. `x-hrc-actor` is not used for authorization.
- `DurableContractWriter` no longer assumes chain 31337. WalletService passes the
  manifest chain ID; otherwise the writer derives it from the configured provider,
  allowing Sepolia/Amoy transaction bytes while retaining chain mismatch guards.
  Reconciliation refreshes existing signed bytes/receipt and never rebroadcasts a
  nonce merely because the chain state is already settled.
- The isolated V2 integration now uses the real Python FastAPI/SQLite service and
  covers sustained/repeated telemetry and exact policy boundaries, SQLite process
  restart, pending and lost-broadcast receipt recovery, SIWE role/ownership attacks,
  immutable evidence and accounting invariants. Browser verification remains
  runtime-blocked when the dashboard proxy has no API/RPC listener.


## Wallet action discoverability (R27, 2026-10-12)

- Customer Start stays visible after login and exposes its blocking reason in
  `walletStartStatus` via `aria-describedby`. `walletStartBlockReason()` drives
  both rendering and the click guard, so a blocked click cannot allocate a new
  pending session reference or request a wallet transaction.
- V2 Start requires a selected ASSIGNED/ACTIVE server assignment. Empty tasks
  tell the user an admin must create/publish; task fetch failures clear stale
  assignments/selections, block Start and preserve the underlying error until a
  successful refresh. Pending start retries keep the existing reference and
  active RUNNING/STOPPING/SETTLEMENT_PENDING sessions cannot start again.
- Enough on-chain allowance renders `Approved <policy amount>` and explains
  that no further approval is needed. Guidance uses the current stake/allowance,
  not a fixed 2 HRC. Approval and Start explanations sit next to the buttons.
- A local task was created/published through the existing SIWE admin HTTP APIs
  in the current runtime; it is local SQLite data, not automatic production
  bootstrapping. The customer must still accept it, and real ESP32 telemetry
  plus independently approved completion remain required. A fresh launcher
  runtime has a separate database and does not inherit this task.
- Verification: API 203/203; browser 15/15 on desktop/mobile, syntax and diff
  checks pass. Browser wallet injection/rendering fixtures do not verify real
  MetaMask extension writes or hardware operation. Current sensor `latest=null`
  is an observed external readiness blocker, not bypassed by the UI change.
