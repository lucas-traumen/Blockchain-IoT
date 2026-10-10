# Pipeline LSTM

Pipeline chạy và báo cáo hiện tại dùng **một model triển khai duy nhất: LSTM**.
Random Forest đã được loại khỏi CLI, evaluator, artifact runtime và cấu hình
Compose. Hai đường persistence và constant velocity chỉ còn là baseline tham
chiếu trong báo cáo sai số; chúng không được nạp trên ESP32 hay backend.

Môi trường AI dùng Python 3.12, TensorFlow 2.20.0 và Keras 3.15.1:

```bash
python3.12 -m venv .venv-ai
.venv-ai/bin/pip install -r requirements-ai.txt
```

`.venv` chạy các test/backend không cần TensorFlow. Runtime LSTM và train/evaluate
cần `.venv-ai` hoặc image `blockchain-hrc:ai`.

## Dataset thực đang dùng

Dataset canonical là `data/processed/real-20261010/dataset.jsonl` với 12.820
mẫu thuộc 49 session. Mười mẫu không hợp lệ vẫn được giữ trong dataset audit.
Các session `sensor_check` được tách sang
`data/processed/real-20261010/sensor-fault-dataset.jsonl`, nên không trộn lỗi
cảm biến vào metric dự báo khoảng cách.

Dataset dành cho LSTM là
`data/processed/real-20261010/lstm-dataset.jsonl`:

- 12.361 mẫu hợp lệ;
- 47 session target `person` đủ độ dài;
- 33/7/7 session cho train/validation/test;
- 7.046/2.740/1.902 cửa sổ tương ứng;
- SHA-256: `e4833031f094c79bff03e5a7320bb4a7075d9fbc66296705f7941727b00a7315`;
- split được cố định trong `split_manifest.json`, trước khi tạo window;
- scaler chỉ fit trên train.

Ngưỡng hiện tại của mô hình mô phỏng cạnh hình vuông là `Sp = 0.04 m = 40 mm`.
Đây là ngưỡng mô phỏng đã chốt cho bộ dữ liệu mới, không phải tuyên bố chứng
nhận an toàn cho robot thật.

## Train và đánh giá

Artifact đã train nằm tại
`ai_model/artifacts/lstm-real-20261010/`, gồm `model.keras`, `scaler.json`,
`split_manifest.json`, `manifest.json` và `metrics.json`.

Train lại từ dataset canonical:

```bash
.venv-ai/bin/python -m ai_model.lstm_train \
  --dataset data/processed/real-20261010/lstm-dataset.jsonl \
  --out-dir ai_model/artifacts/lstm-real-20261010 \
  --epochs 100 --batch-size 32 --seed 20261010
```

Đánh giá đúng test session đã lưu trong artifact:

```bash
.venv-ai/bin/python -m ai_model.evaluate \
  --model-type lstm \
  --dataset data/processed/real-20261010/lstm-dataset.jsonl \
  --model-dir ai_model/artifacts/lstm-real-20261010 \
  --sp 0.04 --horizon-index 4 \
  --output data/results/lstm-only-20261010/evaluation-500ms.json
```

Đánh giá toàn bộ horizon và xuất dự báo cho hình:

```bash
.venv-ai/bin/python -m scripts.evaluate_all_horizons \
  --dataset data/processed/real-20261010/lstm-dataset.jsonl \
  --model-dir ai_model/artifacts/lstm-real-20261010 \
  --sp 0.04 \
  --out data/results/lstm-only-20261010/evaluation-all-horizons.json

.venv-ai/bin/python -m scripts.export_ai_predictions \
  --dataset data/processed/real-20261010/lstm-dataset.jsonl \
  --model-dir ai_model/artifacts/lstm-real-20261010 \
  --out data/results/lstm-only-20261010/predictions.csv
```

Kết quả test hiện tại ở horizon 500 ms:

- LSTM MAE trung bình: **6,46 mm**;
- LSTM RMSE trung bình: **10,68 mm**;
- test có **82 crossing** xuống dưới hoặc chạm 40 mm;
- LSTM chưa phát episode cảnh báo sớm nào: precision `N/A`, recall `0`,
  F1-score `0`, miss rate `1`.

F1 không còn bị `NULL` khi test có event. Precision vẫn là `N/A` trong trường
hợp model không sinh positive nào, vì không có mẫu số predicted-positive;
recall và F1 được ghi bằng 0 để thể hiện model bỏ sót toàn bộ event. Khi một
nhóm test không có crossing, các metric event của nhóm đó vẫn là `N/A`.

Điểm cần ghi trong báo cáo là: **LSTM dự báo khoảng cách khá tốt trên bộ test
này, nhưng khả năng cảnh báo sớm trước Sp = 40 mm chưa đạt**. Không dùng MAE
thấp để kết luận hệ thống cảnh báo đã an toàn.

Ảnh báo cáo nằm tại `data/results/lstm-only-20261010/figures/`, gồm PNG, SVG
và `figures.pdf`. Hình `event_metrics_500ms`, `f1_by_horizon` và
`results_table` đã có chú thích về support event và F1 bằng 0.

## Runtime backend

Compose mặc định dùng image AI, model type LSTM và artifact
`lstm-real-20261010`:

```bash
docker compose up -d --build --wait mqtt iot_backend
curl http://localhost:8001/health
```

Hoặc chạy trực tiếp:

```bash
MODEL_TYPE=lstm \
MODEL_ARTIFACT_DIR=ai_model/artifacts/lstm-real-20261010 \
SAFETY_CONFIG=configs/safety-square.yaml \
.venv-ai/bin/uvicorn iot_backend.main:app --host 127.0.0.1 --port 8000
```

Prediction có trạng thái `WARMUP` trong 9 mẫu đầu, `OK` từ mẫu thứ 10,
`INVALID_WINDOW` khi có lỗi/gap/jitter và `MODEL_UNAVAILABLE` nếu artifact
không thể nạp. `predicted_violation_ms` là horizon đầu tiên có dự báo không
lớn hơn `Sp`; safety đo trực tiếp vẫn có quyền quyết định `CLEAR`,
`VIOLATION` hoặc `UNKNOWN`.

## LSTM trên ESP32

Firmware dùng cùng kiến trúc Input `(10,2)` → LSTM(32) → Dense(32) → Dense(5),
scaler train-only và threshold mặc định 40 mm. Regenerate block trọng số/scaler
khi artifact thay đổi:

```bash
docker run --rm --user 0:0 \
  -v "$PWD:/app" -w /app blockchain-hrc:ai \
  python scripts/generate_esp32_lstm.py \
  --artifact ai_model/artifacts/lstm-real-20261010/model.keras \
  --scaler ai_model/artifacts/lstm-real-20261010/scaler.json \
  --output firmware/esp32/main/lstm_inference.c
```

Artifact hash hiện tại:

- model: `6713054121907c92645924d7e34a13db10867d19871fa161fe747f1fab1a9f0b`;
- scaler: `164c42206f8214f0be20d8788b018a2fcb46c0ef132b3dc53f19b2ff43457734`.

Parity native của firmware dùng chính vector từ artifact mới. Việc compile và
parity không thay thế kiểm thử flash, cảm biến HC-SR04, Wi-Fi, reconnect và
soak test trên board thật.
