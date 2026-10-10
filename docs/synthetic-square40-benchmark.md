# Benchmark LSTM synthetic với Sp = 40 mm

Bộ benchmark dùng để kiểm chứng pipeline LSTM trong mô hình cảm biến đặt trên cạnh hình vuông. Dữ liệu được sinh bằng `scripts/synthetic_square_dataset.py`; không phải số đo từ ESP32/HC-SR04.

## Thiết lập

- `Sp = 0.040 m = 40 mm`
- 10 Hz, cửa sổ 10 mẫu, horizon 100/200/300/400/500 ms
- 400 session, 96.000 mẫu, 240 mẫu/session
- Split theo session trước windowing: 280 train / 60 validation / 60 test
- 51 crossing trong test; các session không crossing vẫn được giữ trong test
- Fault dataset tách riêng, không dùng để tính F1 dự báo khoảng cách
- Mọi dòng có `synthetic: true`; không gộp vào dataset real

## Kết quả chạy pipeline chuẩn

Artifact và report của lần chạy bằng `configs/model.yaml`:

- Model: `ai_model/artifacts/lstm-v1/`
- Report: `data/results/lstm-v1/`
- Dataset hash: `0823c297f9248cce2dcb49dab682a9c24ce19a6aebda9e84f3838485de67d6d8`

| Metric tại horizon 500 ms | Trước debounce | Sau xác nhận 2/3 |
|---|---:|---:|
| MAE trung bình 5 horizon | 2.56 mm | — |
| RMSE trung bình 5 horizon | 3.24 mm | — |
| Precision | 100.00% | 100.00% |
| Recall | 88.24% | 64.71% |
| F1-score | 93.75% | 78.57% |
| Miss rate | 11.76% | 35.29% |
| Lead time trung bình | 193.5 ms | 124.2 ms |

`F1 = 0,9375` là metric episode cảnh báo offline trước debounce. `F1 = 0,7857` là kết quả replay qua detector 2/3. Đây là kết quả benchmark mô phỏng, không thay thế đánh giá trên dữ liệu ESP32 thật.

Lần chạy hoàn thành 11 epoch và khôi phục checkpoint val_loss tốt nhất ở epoch 1.
Validation loss tăng trong các epoch sau; đường học được giữ nguyên trong ảnh.
Sau detector 2/3 còn 18/51 crossing bị bỏ sót. Constant-velocity có F1 0,9402 tại
500 ms, nên không kết luận LSTM thắng mọi baseline.

## Ảnh và tài liệu

- `data/results/lstm-v1/figures/event_outcomes.png`
- `data/results/lstm-v1/figures/event_metrics_500ms.png`
- `data/results/lstm-v1/figures/f1_by_horizon.png`
- `data/results/lstm-v1/figures/regression_errors.png`
- `data/results/lstm-v1/figures/training_history.png`
- `data/results/lstm-v1/figures/figures.pdf`
- `data/results/lstm-v1/verification.json`

Đây là benchmark synthetic-only dùng cho demo; không có report model real song
song trong runtime canonical.
