# Báo cáo hiệu chuẩn HC-SR04

Tool tạo đề xuất `Z_d` từ lỗi đo đã quan sát, giữ session/target/điều kiện thử
và SHA-256 CSV đầu vào. Tool không sửa safety config và không chứng nhận độ
an toàn của cảm biến/robot.

## Thu dữ liệu trên board

Các bước này cần thiết bị và dụng cụ đo thật, chưa được thực hiện trong phiên
code: build/flash firmware, kiểm điện áp Echo sau chia áp, bố trí mặt mục tiêu
ở nhiều khoảng cách trong vùng thử 0.10–1.00m. Ghi khoảng cách tham chiếu, mẫu
đo lặp lại, loại bề mặt, góc, nhiệt độ, nguồn cấp và session. Giữ cả timeout
với `valid=false`. Mỗi điều kiện nên có nhiều phép đo; một mẫu không đủ tính
sigma. Không trộn các nguồn real/synthetic trong một report.

CSV bắt buộc các cột context; `valid` dùng true/false hoặc 1/0. Ví dụ dưới
đây là fixture **synthetic**, không phải số đo thí nghiệm:

```csv
reference_mm,measured_mm,valid,session_id,target_type,source,conditions
500,501,true,cal_demo,flat,synthetic,fixture only
500,502,true,cal_demo,flat,synthetic,fixture only
500,503,true,cal_demo,flat,synthetic,fixture only
500,,false,cal_demo,flat,synthetic,fixture only
```

```bash
.venv/bin/python -m scripts.calibrate --input data/calibration.csv \
  --output data/calibration-report.json --sigma-multiplier 3
```

## Công thức và cách đọc

Với `e_i = measured_mm - reference_mm`:

- Bias là trung bình có dấu của `e_i`; MAE là trung bình `|e_i|`.
- Sigma là độ lệch chuẩn mẫu (`n-1`), null ở nhóm chỉ có một mẫu.
- Đề xuất mỗi nhóm/aggregate là
  `max(|bias| + k × sigma, max |e_i|) / 1000` mét.
- `recommended_zd_m` lấy giá trị lớn nhất giữa aggregate và các nhóm có sigma.
  Các nhóm tách theo reference, session, target, source và conditions.
- Mẫu invalid được đếm, không đưa vào bias/MAE/sigma; valid NaN/inf/thiếu số
  hoặc đo ngoài miền telemetry bị từ chối.

`k=3` là lựa chọn thực nghiệm của tool, không tự mang ý nghĩa xác suất hay
chứng nhận ISO. Đề xuất chỉ áp dụng cho điều kiện đã đo; việc sử dụng trong
cấu hình cần đánh giá vùng đo và điều kiện vận hành. Khi chấp nhận một report,
người vận hành tạo phiên bản `configs/safety.yaml` với `valid_from` mới; giữ
config demo/lịch sử để replay không đổi. Không giảm Sp để vừa vùng đo.
