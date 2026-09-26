# PROJECT_MEMORY — quyết định bền vững

Repo: HRC Safety Log (Blockchain-IoT). Phạm vi: TV3 + TV4. TV1–TV2 (Smart Contract, hash, dashboard, bộ gom sự kiện) ngoài phạm vi, chỉ bàn giao qua assessment API/outbox.

## Kiến trúc chốt

- Luồng: ESP32 + HC-SR04 → MQTT (Mosquitto) → Python backend (FastAPI + paho-mqtt thread) → SQLite WAL (`data/hrc.db`) → safety + inference → `processed_samples` (bàn giao TV2 qua REST/export).
- Không dùng ROS2, không robot 3D, không tay đòn cơ khí. AI chạy trên backend; ESP32 chỉ đo và truyền.
- Backend Python thống nhất (không dựng Node trùng chức năng — repo khởi đầu trống nên chọn một stack).
- Chạy từ repo root, không cài package: `backend`, `ai_model`, `scripts` là package top-level. Venv `.venv/`.

## Quy ước dữ liệu

- Khóa mẫu: `(device_id, boot_id, sequence)`; UNIQUE trong `raw_samples` và `processed_samples` (+ `processor_version`).
- Đơn vị nội bộ SI (m, s, m/s); payload telemetry dùng mm/µs; đổi ở biên.
- Timestamp: `measured_at` ISO-8601 UTC (ms) tại lúc bắt đầu đo; chưa sync SNTP → null + `time_synced=false`. `monotonic_us` để tính khoảng thời gian giữa mẫu.
- Topic: `hrc/v1/devices/{device_id}/telemetry|control|ack`, QoS 1, retain=false.
- Session: khóa bằng ack từ thiết bị (boot_id + sequence tại lệnh start); fallback: max sequence đã nhận. Gán mẫu theo khoảng sequence cùng boot. Không gán theo giờ nhận.
- Schema version "1.0" cho telemetry/assessment/control/ack; file JSON Schema ở `schemas/` là nguồn chân lý, DDL SQLite ở `backend/ingestion/schema.sql`.

## Safety & model

- Công thức Sp chuẩn (SSM một chiều, robot phanh đều): `Sh=vh(Tr+Ts)`, `Sr=vr·Tr`, `Ss=vr·Ts/2`, `Sp=Sh+Sr+Ss+C+Zd+Zr`. Tr/Ts là tham số robot giả định, không lấy độ trễ Blockchain. Fixture: vh=1, vr=0.2, Tr=0.1, Ts=0.2, C=0.05, Zd=0.03, Zr=0.02 → Sp=0.44.
- Trạng thái: margin>0 → CLEAR, ≤0 → VIOLATION, lỗi → UNKNOWN (không bao giờ "nâng" UNKNOWN thành CLEAR, không tự giảm Sp).
- Cấu hình safety có `config_version` + `valid_from`; replay dùng config đúng thời điểm mẫu (fallback `received_at` khi chưa sync, ghi `config_time_source`).
- Model v1: RandomForestRegressor multi-output, window 10 mẫu @100ms, horizons 100–500ms. Feature chỉ khoảng cách quá khứ (không sequence/session/scenario/future). Baseline persistence + constant-velocity so sánh cùng split. Không hứa thắng baseline — báo kết quả thật.
- Windowing/feature dùng chung train + runtime qua `ai_model/windows.py` + `ai_model/features.py`.

## Môi trường đã xác minh (2026-09-21)

- Python 3.12.3; `.venv` đã tạo với: paho-mqtt 2.1.0, fastapi 0.141.1, uvicorn 0.53.0, pydantic 2.13.5, jsonschema 4.26.0, PyYAML 6.0.3, scikit-learn 1.9.1, pandas 3.0.6, numpy 2.5.3, joblib 1.6.0, pytest 9.1.1, python-dotenv 1.2.3, httpx 0.28.1, scipy 1.18.1.
- Không có ESP-IDF/xtensa toolchain và không có mosquitto binary trên máy này → firmware chỉ review được (không build); test backend không phụ thuộc broker thật (inject trực tiếp qua `IngestionService.handle_message`); broker qua docker `eclipse-mosquitto` khi chạy thật (docker 29.1.3 có sẵn).
- Người dùng có group dialout (flash serial OK).

## Giới hạn đã cam kết

- Demo hộp đen tham chiếu SSM, tham số robot giả định phần mềm — không tuyên bố đạt ISO.
- Không ghi Blockchain tức thì trong TV3/TV4 (đó là TV1–TV2), nhưng bảo toàn timestamp và thứ tự đo.
- Dữ liệu synthetic phải ghi nhãn `synthetic`, không trình bày như kết quả thực nghiệm. Firmware chưa build/flash trên máy này — ghi rõ trong README/docs phần chưa kiểm chứng.

## Bài học quy trình & kỹ thuật M4 (2026-09-22)

- **Task cho glm-5.3-flash phải nhỏ**: task firmware nguyên khối (~10 file C) trả kết quả rỗng 3 lần liên tiếp dù provider sống (diagnostic PONG). Tách 4 giai đoạn (khung → driver → mạng → payload/integration, mỗi task ~2–3 file) thì chạy ngay và chất lượng tốt. M5+ chia task theo nhóm file ≤ ~500 dòng code sinh ra.
- **Lớp review độc lập bắt được bug orchestrator bỏ sót**: reviewer đối chiếu source esp-mqtt pin commit của IDF v5.5 phát hiện `esp_mqtt_client_register_event` (số ít, KHÔNG phải số nhiều) — orchestrator đọc 100% code vẫn kiểm sai. Giữ pattern review độc lập sau mỗi mốc lớn.
- **ESP-IDF v5.5 API đã xác minh** (qua review đối chiếu source): `esp_mqtt_client_register_event`; `.outbox.limit` tính bằng bytes (KHÔNG có `.task.outbox_limit`); `esp_mqtt_client_enqueue(..., store=true)` non-blocking cho telemetry chu kỳ cao; `CONFIG_LWIP_SNTP_MAX_SERVERS` default 1 (muốn NTP dự phòng phải set trong sdkconfig.defaults).
- **RMT RX (driver HC-SR04)**: khi line không có cạnh nào sau TRIG, phiên nhận không tự kết thúc → receiver kẹt trạng thái RUN → phải `rmt_disable` + `rmt_enable` reset. Idle threshold cắt pulse đang giữ mức; giá trị pulse bị cắt là clamp, không dùng làm echo_us.
- **Ngữ nghĩa error_code firmware (chốt sau M4e)**: `ECHO_STUCK_HIGH` = ECHO cao TRƯỚC khi đo (precondition); `ECHO_TIMEOUT` = echo có cạnh lên nhưng không xuống trong timeout (echo_us=null ở driver — gồm xung "không có vật" ~38ms; ở mức telemetry, echo hoàn tất 26.2–30ms → distance > 4500 → ECHO_TIMEOUT giữ echo_us thật); `ECHO_STUCK_LOW` = không có cạnh lên nào. Lệnh control lạ/thiếu session_id → KHÔNG ack (ack chỉ dành cho lệnh hợp lệ).
