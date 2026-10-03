# Pipeline LSTM

Môi trường AI riêng dùng Python 3.12, TensorFlow 2.20.0, Keras 3.15.1:

```bash
python3.12 -m venv .venv-ai
.venv-ai/bin/pip install -r requirements-ai.txt
```

`.venv` giữ baseline RF/backend. LSTM live chạy toàn backend bằng `.venv-ai`;
không có inference service/network hop riêng. Import các module ai_model và
backend không tự import TensorFlow; chỉ train/load LSTM mới cần thư viện này.

## Dữ liệu và preprocessing

JSONL dùng telemetry cùng metadata `session_label` hoặc `session_id`,
`scenario`, `target_type`, `speed_level`, `synthetic`. Session khác nhau phải
có ID khác nhau. Export raw từ session_cli chỉ có 11 field telemetry, nên
không đưa nhiều file thô thiếu session boundary trực tiếp vào trainer. Dùng:

```bash
.venv/bin/python -m scripts.export_dataset --db data/hrc.db \
  --out data/sessions.jsonl --source real
```

Tool yêu cầu session đã đóng, thêm session label/scenario/target_type từ
session, không lấy mẫu mồ côi. Simulator dùng `--source synthetic` và DB riêng;
boot `synthetic_*`/`sim_*` không được export với nhãn real.

Chia session 70/15/15 trước khi windowing. Cửa sổ giữ cùng session/device/boot,
sequence liên tiếp, nhịp `0.1 ± 0.03s`, gap không quá 0.25s. Sample lỗi cắt
run. Train/runtime dùng chung features: distance mét + delta mét, delta đầu
window bằng 0. Scaler chỉ fit train; validation chọn checkpoint, test báo cáo
sau fit. Output inverse scaling về mét trước metric/threshold, không round.

## Train và evaluator

```bash
.venv-ai/bin/python -m ai_model.lstm_train --dataset data/sessions.jsonl \
  --out-dir ai_model/artifacts/lstm-v1 --epochs 100 --batch-size 32 --seed 42
.venv-ai/bin/python -m ai_model.evaluate --dataset data/sessions.jsonl \
  --model-type lstm --model-dir ai_model/artifacts/lstm-v1 \
  --output data/report.json
```

Model Input(10,2) → LSTM32 → Dropout0.2 → Dense32 ReLU → Dense5, Adam/Huber.
EarlyStopping, ReduceLR và ModelCheckpoint đọc từ `configs/model.yaml`.
Artifact: `model.keras`, `scaler.json`, `split_manifest.json`, `manifest.json`,
`metrics.json`. Trainer ghi staging, promote manifest cuối; loader từ chối
bundle thiếu/sai hash/shape/version. Manifest mới thêm hash metrics, loader vẫn
đọc artifact M11a cũ không có hash metrics nếu JSON/version/count hợp lệ.

Evaluator đòi dataset SHA-256 và split/session/window counts khớp artifact,
chỉ predict test. Báo MAE/RMSE từng horizon, precision/recall/F1, false-warning
rate, miss rate, lead time ms và breakdown scenario/target_type. Metric không
xác định trả null. Không match sự kiện/cảnh báo xuyên device/boot/run/session.
`--horizon-index` chọn horizon cho event metrics (mặc định 4 = 500ms).

Thêm `--rf-model-dir ai_model/artifacts` để so RF trên chính windows test
LSTM. Evaluator kiểm dataset hash, session lists train/val/test và window/
horizon config RF khớp LSTM; lệch hoặc artifact RF cũ thiếu dataset hash thì
bị từ chối. Train lại RF trên cùng dataset bằng `ai_model.train` trước khi so.
Hai baseline persistence/constant-velocity luôn có.

## Runtime và replay

```bash
MODEL_TYPE=lstm MODEL_ARTIFACT_DIR=ai_model/artifacts/lstm-v1 \
  .venv-ai/bin/uvicorn backend.main:app --host 127.0.0.1 --port 8000
.venv-ai/bin/python -m scripts.replay --db data/hrc.db \
  --model-type lstm --model-dir ai_model/artifacts/lstm-v1 \
  --processor-version replay-lstm-v1
```

LSTM buffer riêng theo device/boot/session, reset khi đổi boot/session, lỗi,
gap hoặc jitter. 1–9 mẫu WARMUP, mẫu 10 OK. Mẫu lỗi/đứt run INVALID_WINDOW;
loader/inference lỗi MODEL_UNAVAILABLE. Replay gán lại session từ ranh giới DB
và giữ mode offline; không báo stale do giờ máy hiện tại. Catch-up prime buffer
bằng raw đã processed trước khi tính mẫu pending, state event nằm trong DB.

`predicted_violation_ms` là horizon đầu có dự báo <= Sp, hoặc null khi không
có/không xác định được Sp. `early_warning` chỉ true khi hiện tại > Sp.
Prediction không thay safety.status. Chọn `MODEL_TYPE=rf` để rollback rõ ràng;
RF giữ field cũ và cũng so ngưỡng trên float không round.

Khi máy giới hạn thread, có thể dùng cùng các biến đã dùng trong test:
`TF_NUM_INTEROP_THREADS=1 TF_NUM_INTRAOP_THREADS=1 OMP_NUM_THREADS=1`.
Artifact/dataset/model chưa được kiểm chứng với board thật.
