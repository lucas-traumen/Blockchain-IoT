# Pipeline LSTM canonical

Repository này dùng một artifact LSTM duy nhất cho demo và runtime:

```text
ai_model/artifacts/lstm-v1/
data/results/lstm-v1/
```

Artifact được train trên dataset synthetic square-40mm, không phải dữ liệu đã
hiệu chuẩn từ ESP32/HC-SR04. Persistence và constant velocity chỉ là baseline
offline; chúng không được nạp vào backend hoặc firmware.

## Dataset và artifact

Dataset nguồn đã khóa tại:

```text
data/synthetic/square-40mm-20261010/dataset.jsonl
```

Thiết lập:

- `Sp = 0.040 m = 40 mm`;
- 10 Hz, window 10 mẫu;
- horizon `100/200/300/400/500 ms`;
- split theo session `280/60/60` trước khi tạo window;
- scaler chỉ fit trên train;
- mọi dòng có nhãn `synthetic=true`.

Artifact canonical gồm đúng năm file:

```text
model.keras
scaler.json
split_manifest.json
manifest.json
metrics.json
```

Manifest đối soát model hash, scaler hash, dataset hash, split hash, shape
`(10,2) -> (5,)`, preprocessing `pp-lstm-v1` và số window từng split.

## Train lại

```bash
docker compose --profile ai build ai

docker compose run --rm --no-deps ai python -m ai_model.lstm_train \
  --dataset data/synthetic/square-40mm-20261010/dataset.jsonl \
  --config configs/model.yaml \
  --split-manifest data/synthetic/square-40mm-20261010/split_manifest.json \
  --out-dir ai_model/artifacts/lstm-v1 \
  --seed 20261010
```

Không tạo artifact vào thư mục có ngày/nguồn khác. Muốn thử model mới, dùng
thư mục tạm ngoài `ai_model/artifacts/lstm-v1`, kiểm tra đầy đủ rồi mới
promote bằng cách train lại artifact canonical.

## Đánh giá và report

```bash
mkdir -p data/results/lstm-v1

docker compose run --rm --no-deps ai python -m ai_model.evaluate \
  --model-type lstm \
  --dataset data/synthetic/square-40mm-20261010/dataset.jsonl \
  --model-dir ai_model/artifacts/lstm-v1 \
  --config configs/model.yaml \
  --sp 0.04 --horizon-index 4 \
  --output data/results/lstm-v1/evaluation-500ms.json

docker compose run --rm --no-deps ai python -m scripts.evaluate_all_horizons \
  --dataset data/synthetic/square-40mm-20261010/dataset.jsonl \
  --model-dir ai_model/artifacts/lstm-v1 \
  --config configs/model.yaml \
  --sp 0.04 \
  --out data/results/lstm-v1/evaluation-all-horizons.json

docker compose run --rm --no-deps ai python -m scripts.export_ai_predictions \
  --dataset data/synthetic/square-40mm-20261010/dataset.jsonl \
  --model-dir ai_model/artifacts/lstm-v1 \
  --config configs/model.yaml \
  --out data/results/lstm-v1/predictions.csv

docker compose run --rm --no-deps ai python -m scripts.plot_ai_results \
  --report data/results/lstm-v1/evaluation-500ms.json \
  --all-horizons data/results/lstm-v1/evaluation-all-horizons.json \
  --metrics ai_model/artifacts/lstm-v1/metrics.json \
  --dataset data/synthetic/square-40mm-20261010/dataset.jsonl \
  --predictions data/results/lstm-v1/predictions.csv \
  --out-dir data/results/lstm-v1/figures

docker compose run --rm --no-deps ai python -m scripts.verify_synthetic_benchmark \
  --dataset-dir data/synthetic/square-40mm-20261010 \
  --model-dir ai_model/artifacts/lstm-v1 \
  --report-dir data/results/lstm-v1
```

`data/results/lstm-v1/` là report duy nhất được dùng cho demo. Các metric phải
giữ nhãn synthetic-only và không được diễn giải thành chứng nhận an toàn phần
cứng. F1/recall sau detector 2/3 được báo riêng với metric regression.

## Runtime

Compose mặc định dùng artifact canonical:

```bash
docker compose up -d --build --wait mqtt iot_backend
curl http://localhost:8001/health
```

Hoặc chạy trực tiếp:

```bash
MODEL_TYPE=lstm \
MODEL_ARTIFACT_DIR=ai_model/artifacts/lstm-v1 \
SAFETY_CONFIG=configs/safety-square.yaml \
.venv-ai/bin/uvicorn iot_backend.main:app --host 127.0.0.1 --port 8000
```

Prediction có `WARMUP` trong chín mẫu đầu, `OK` từ mẫu thứ mười,
`INVALID_WINDOW` khi có lỗi/gap/jitter và `MODEL_UNAVAILABLE` nếu artifact
không thể nạp. Safety đo trực tiếp vẫn độc lập với model.

## Firmware

Sau khi artifact thay đổi, regenerate source từ đúng artifact canonical:

```bash
docker compose run --rm --no-deps --user 0:0 ai \
  python scripts/generate_esp32_lstm.py \
  --artifact ai_model/artifacts/lstm-v1/model.keras \
  --scaler ai_model/artifacts/lstm-v1/scaler.json \
  --output firmware/esp32/main/lstm_inference.c
```

Build/native parity không thay thế flash, đo HC-SR04, Wi-Fi, reconnect và soak
trên board thật.
