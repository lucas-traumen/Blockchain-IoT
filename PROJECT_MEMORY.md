# PROJECT_MEMORY — quyết định bền vững

Repo: HRC Safety Log (Blockchain-IoT). Phạm vi: TV3 + TV4. TV1–TV2 (Smart Contract, hash, dashboard, bộ gom sự kiện) ngoài phạm vi, bàn giao assessment/event package qua API; không có blockchain outbox trong phạm vi này.

## Kiến trúc chốt

- Luồng: ESP32 + HC-SR04 → MQTT (Mosquitto) → Python backend (FastAPI + paho callback/raw commit + worker compute) → SQLite WAL (`data/hrc.db`) → safety + inference → `processed_samples` (bàn giao TV2 qua REST/export).
- Không dùng ROS2, không robot 3D, không tay đòn cơ khí. AI chạy trên backend; ESP32 chỉ đo và truyền.
- Backend Python thống nhất (không dựng Node trùng chức năng — repo khởi đầu trống nên chọn một stack).
- Chạy từ repo root, không cài package: `backend`, `ai_model`, `scripts` là package top-level. Venv `.venv/` là lựa chọn Python trực tiếp.
- Người dùng chọn Docker cho host service (2026-10-02): Compose `blockchain-hrc`
  quản lý Mosquitto1885/API8001, backend dùng mqtt:1883 trong mạng Docker;
  named volumes giữ SQLite và broker. RF image không TF; AI profile/image
  phục vụ train/evaluate và backend LSTM khi chọn tường minh.

## Quy ước dữ liệu

- Khóa mẫu: `(device_id, boot_id, sequence)`; UNIQUE trong `raw_samples` và `processed_samples` (+ `processor_version`).
- Đơn vị nội bộ SI (m, s, m/s); payload telemetry dùng mm/µs; đổi ở biên.
- Timestamp: `measured_at` ISO-8601 UTC (ms) tại lúc bắt đầu đo; chưa sync SNTP → null + `time_synced=false`. `monotonic_us` để tính khoảng thời gian giữa mẫu.
- Topic: `hrc/v1/devices/{device_id}/telemetry|control|ack`, QoS 1, retain=false.
- Session: khóa bằng ack từ thiết bị (boot_id + sequence tại lệnh start); fallback: max sequence đã nhận. Gán mẫu theo khoảng sequence cùng boot. Không gán theo giờ nhận. Trong lúc START/STOP chờ ACK, SessionManager giữ boundary gate; MQTT chỉ commit raw đồng bộ rồi worker mới compute, để assessment/predictor/event cùng scope.
- Schema version "1.0" cho telemetry/assessment/control/ack; file JSON Schema ở `schemas/` là nguồn chân lý, DDL SQLite ở `backend/ingestion/schema.sql`.

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
- Quyết định môi trường (M13 chốt 2026-10-02): `.venv` giữ RF/backend không TF;
  environment AI riêng (`requirements-ai.txt`: TF2.20.0/Keras3.15.1 + backend
  dependencies) chạy cả train/evaluate và backend LSTM. Chọn `MODEL_TYPE=rf`
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
  mapping có `session_id`; preprocessing module không phụ thuộc backend/DB.
- M11a đã bổ sung `ai_model.lstm_train` với hai API độc lập `train_lstm` và
  `load_lstm_artifact`; không thay thế `ai_model.train` RF. TensorFlow được
  import lazy để backend `.venv` vẫn chạy không cần TF. Trainer đọc khối
  `lstm` trong YAML, gọi M10 split-before-windowing, fit `LSTMScaler` chỉ trên
  train, train Input `(10,2)` → LSTM/Dropout/Dense → `(5)`, tính MAE sau
  inverse-transform về mét, rồi validate manifest/timestamp trước khi promote
  staging. Artifact bắt buộc và thứ tự kiểm tra loader: `model.keras`,
  `scaler.json`, `split_manifest.json`, `manifest.json`, `metrics.json`; hash
  model/scaler/split và source dataset được đối soát. `scaler_sha256` là
  optional trong schema để tương thích fixture cũ nhưng bắt buộc trong artifact
  LSTM thực tế.
- Bằng chứng M11a (2026-09-30): backend `.venv` `tests/test_model.py
  tests/test_inference.py tests/test_integration.py` = 23 passed; full suite =
  214 passed, 8 skipped, 2 dependency warnings; AI environment TF 2.20.0 /
  Keras 3.15.1 chạy `tests/test_lstm_train.py` = 13 passed, 1 skipped; smoke
  độc lập = 36/36, gồm năm artifact, hash, shape, train-only scaler, finite
  metrics, tamper/missing-file rejection. Artifact chỉ nằm dưới `/tmp/opencode`.
- M11a review không có blocker/major. Note tại M11a về zero-ratio/metric
  config/metrics integrity/import backend đã xử lý ở M12b (2026-10-02): ratios
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
- Không có ESP-IDF/xtensa toolchain và không có mosquitto binary trên máy này → firmware chỉ review được (không build); test backend không phụ thuộc broker thật (inject trực tiếp qua `IngestionService.handle_message`); broker qua docker `eclipse-mosquitto` khi chạy thật (docker 29.1.3 có sẵn).
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
  khác tối đa 0.0. Không thay đổi requirements backend; tái lập bằng
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
- Full test cuối: backend **310 pass/11 skip**, AI **321 pass/0 skip**. Live
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
- Compose default ports1885/8001 để tránh service dự án khác. MQTT/backend
  đang chạyhealthy; containers RF/AI pipcheckPASS, AI artifact integration
  58pass. AI UID/GID theo host, configs/artifacts backend readonly; không
  tự dùng artifact synthetic làm model mặc định.
- Smoke Docker: 50raw đầy đủ, device_ack START/STOP, persistenceSQLite và
  retainedMQTT qua restart; broker-onlyrestart backend tự reconnectPASS.
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
  serial M4g. Điền `iot_code/esp32/main/network_config.local.h` (ignored,
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
  Full backend **358 passed/11 skipped**, 2 dependency warnings. Actual C
  strict compiler và stub FreeRTOS enforce include order; fixture tách khỏi
  local credentials. Wi-Fi/NVS mock không chứng minh radio bất đồng bộ.
- Build IDF6.0.1 PASS S3 **949680B**, ESP32 **937808B**, size checks PASS,
  không compiler warnings/errors. Evidence trong `/tmp/blockchain-firmware-check/`:
  `wifi-list-{esp32s3,esp32}-build.log` và `wifi-list-pytest.log`. ESP32
  dùng checkout tạm, repo giữ sdkconfig S3. Tên serial port có thể đổi;
  chọn chip/cổng thực tế trước flash, không suy chip từ ttyACM/ttyUSB.
- MQTT/backend Docker vẫn healthy. Chưa flash bản M4h; cần thử scan/IP/MQTT,
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
