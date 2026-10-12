# Tự theo dõi hoạt động từ khoảng cách

Task mới dùng `sensor_activity`: theo dõi thời gian người dùng có chuyển động
trước cảm biến, thay cho chu kỳ robot do operator ghi nhận. Mặc định yêu cầu
20 giây hoạt động và ít nhất 30 giây thời gian phiên. Admin có thể đặt mục tiêu
hoạt động khi tạo task; sau publish/accept, quy tắc trong snapshot không đổi.
Task robot-cycle đã công bố giữ nguyên quy tắc cũ. Chọn task hoạt động mới để
sử dụng cơ chế tự động. Không cộng thời gian chờ trang hay thời gian đứng yên.

## Cách dùng

1. Chọn và accept task có dòng **active movement (automatic)**.
2. Approve stake, Start Task Session.
3. Di chuyển lại gần/ra xa cảm biến trong vùng đo, giữ khoảng cách cao hơn
   safety threshold. Theo dõi **ACTIVE MOVEMENT TIME**, thời gian đứng yên và
   trạng thái MOVING / NO MOVEMENT / WAITING FOR VALID SENSOR DATA.
4. Khi đủ mục tiêu, giao diện báo **Activity target reached automatically**.
   Đạt ít nhất thời gian phiên tối thiểu rồi End. Backend chốt bằng chứng và
   contract quyết định số thưởng theo curve, pool và hạn mức ngày.

Thời gian hoạt động là một điều kiện hoàn thành. Curve vẫn dùng thời gian phiên:
base 0.001 HRC sau thời gian tối thiểu, tăng đến tối đa 0.002 HRC tại thời gian
full reward và giữ mức trần sau đó. Đạt hoạt động vẫn cần telemetry hợp lệ, đủ
coverage và không có vi phạm được xác nhận để được thưởng. Chưa đủ hoạt động
trong phiên an toàn: hoàn 0.2 HRC stake, thưởng 0, penalty 0. Vi phạm được xác
nhận và sensor fault tiếp tục dùng quy tắc an toàn hiện có.

## Quy tắc đo demo

- Policy cố định `distance-activity-v1`: median 5 mẫu liên tiếp, cửa sổ 1 giây,
  khoảng cách sau lọc biến thiên ít nhất 10 mm để đánh dấu chuyển động.
- Chỉ khoảng thời gian có mẫu liên tiếp valid, time_synced và assessment CLEAR
  được xét. Interval lớn hơn 0.5 giây, sequence gap, boot change, thiếu
  assessment, lỗi đo hoặc dữ liệu unsafe reset bộ lọc và không cộng hoạt động.
- Chờ đủ mẫu lọc mới bắt đầu phân loại. Nhiễu nhỏ và spike đơn không tạo
  hoạt động. Cửa sổ có thể giữ trạng thái MOVING tối đa khoảng 1 giây sau một
  chuyển động đã đo; đây là thời gian hoạt động theo cửa sổ, không phải số bước.
- Tính từ raw samples gắn với đúng session và processor version; request lặp
  không cộng thêm. Đồng hồ monotonic xác định duration; chờ không có mẫu không
  sinh thêm thời gian. Giao diện đánh dấu UNKNOWN khi timestamp cũ >2 giây.
- Backend tự xác nhận từ evidence ở End; client/admin không thể gửi tổng hoạt
  động hay dùng đường manual approval/cycle để vượt điều kiện.
- End đóng boundary, chốt stats, activity và hash. Retry/restart không tính lại
  kết quả đã chốt và không sửa các settlement cũ.

Một cảm biến khoảng cách chỉ ghi nhận biến thiên của vật trong vùng đo. Nó chưa
xác minh người, số bước hay đường đi vòng. Đi ngang với khoảng cách không đổi
có thể không được tính; chuyển động của một vật khác cũng có thể được cảm biến
thấy. Quy tắc này phù hợp demo hoạt động theo khoảng cách, chưa phải bằng chứng
chống gian lận danh tính.
