# Theo dõi chu kỳ robot trong task V2

Task mới chuyển sang thời gian hoạt động tự động theo yêu cầu người dùng (R33).
Xem [hướng dẫn hoạt động](user-activity.md). Nội dung dưới đây chỉ áp dụng cho
task `robot_cycle_count` đã công bố trước đó.

Một chu kỳ là một lần robot hoàn thành hành động được mô tả trong task. Ví dụ:
gắp vật, di chuyển, đặt vật và trở về vị trí đầu là một lượt nếu đó là hành
động task quy định. Mẫu đo khoảng cách của ESP32 không phải chu kỳ robot.

Bộ đếm hiện tại là **operator ghi nhận**. Firmware chỉ gửi khoảng cách, chưa có
tín hiệu robot hoàn tất hành động để tự đếm. Đứng yên, có trạng thái CLEAR hoặc
đủ thời gian không tự tăng số lượt.

## Cách dùng

1. Customer chấp nhận/chọn task, approve stake và Start Task Session.
2. Operator/admin đăng nhập ví có role đó, mở **Task and completion review**.
   Session đang chạy có số lượt hiện tại và số còn thiếu.
3. Sau khi quan sát robot hoàn thành một hành động, operator bấm
   **Record +1 completed cycle** của đúng session. Mỗi lần bấm ghi một lượt.
4. Customer thấy **Completed robot cycles**, ví dụ `7 / 20` và
   `13 cycles remaining`; dữ liệu được cập nhật mỗi khoảng 5 giây hoặc bằng
   Refresh wallet.
5. Khi đủ mục tiêu, hệ thống tự lưu bằng chứng hoàn thành từ các lượt operator
   đã ghi. Customer kiểm tra trạng thái verified rồi End Session.

Đủ chu kỳ vẫn cần đạt thời gian tối thiểu và các điều kiện an toàn/chất lượng
cảm biến. Ví dụ task 30s minimum, full reward ở 50s: đạt chu kỳ và bằng chứng
hợp lệ ở hoặc sau 50s thì mức thưởng tối đa là 0.002 HRC, tùy reward pool và
hạn mức ngày. Thời gian vượt ngưỡng full reward không làm phiên mất điều kiện.

Nếu chưa đủ chu kỳ mà End, phiên an toàn được hoàn stake, thưởng 0. Thiếu chu kỳ
không tự tạo penalty; penalty cần bằng chứng vi phạm được xác nhận.

## Retry và bằng chứng

Retry recorded cycle dùng lại mã lượt đã gửi. Mất response hoặc reload trang
không cộng lượt đó lần thứ hai. Không ghi thêm lượt sau End hoặc sau khi bằng
chứng hoàn thành đã chốt.

**Final manual completion approval** là đường tương thích để xác nhận một tổng
đã quan sát. Nó chốt bằng chứng ngay; khi đã có các lượt ghi riêng, tổng nhập
phải đúng bộ đếm lưu trong hệ thống. Để theo dõi công việc đang tiếp diễn, dùng
Record +1 thay vì chốt tổng chưa đủ mục tiêu.

Task đã publish và assignment/session đã nhận giữ nguyên mục tiêu trong policy
snapshot. Tạo draft mới nếu cần mục tiêu khác; không sửa số chu kỳ của phiên cũ.

API:

```text
POST /api/sessions/:sessionId/cycles
Authorization: Bearer <SIWE operator/admin token>
{"taskId":"task-...","eventId":"robot-action-..."}
```

Client không gửi observedCycles, thưởng, outcome hoặc địa chỉ hưởng thưởng.
