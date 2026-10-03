# HRC Safety Log — firmware ESP32 / ESP32-S3

Firmware ESP-IDF đo HC-SR04 bằng RMT RX (1 µs), phát telemetry QoS1 mỗi 100 ms,
nhận control và trả ack ranh giới session. Theo yêu cầu ngày 2026-10-02,
Wi-Fi khai báo bằng macro thành mảng hai chiều `{SSID, password}`; firmware
tự quét, chọn AP phù hợp và reconnect. Cấu hình macro là cách vận hành chính;
serial provisioning/NVS giữ cho chế độ cũ nếu chọn `HRC_WIFI_LIST_MODE=0`.

```text
HC-SR04 → RMT RX → telemetry v1 → MQTT → backend
                    ↑ mailbox ← control
                    └→ ack → MQTT
network_config.local.h → SSID/password matrix → scan/select/retry
```

## Trạng thái kiểm chứng

- Build ESP-IDF **6.0.1**, Xtensa GCC 15.2.0: **esp32s3 và esp32 PASS**.
  App, bootloader và partition size checks đều pass. MQTT managed
  `espressif/mqtt` **1.0.0**, GPIO/RMT/UART/USB driver dependencies khai báo
  trực tiếp. Nhánh IDF 5.5 giữ bundled MQTT; chưa build IDF 5.5 trong phiên này.
- Trong phiên ngày 2026-10-02, esptool đã nhận diện **ESP32-D0WD-V3,
  revision v3.1** ở `/dev/ttyUSB0` và **ESP32-S3 revision v0.2** ở
  `/dev/ttyACM0`. Cổng có thể đổi khi cắm lại hoặc đổi bo; chọn target theo
  **chip** và chọn cổng thực tế trước khi flash.
- Người dùng đã cung cấp log HRC chạy thật với ECHO thấp và broker không
  truy cập được. Bản sửa mới chưa được flash/chạy đo bởi Codex. Test native
  chạy module C thật với GPIO/RMT/Wi-Fi/NVS giả lập; không thay thế việc thử
  radio, điện áp Echo, SNTP, chu kỳ đo, reconnect và soak trên bo.
- Build S3 ở `build/`; bản build ESP32 được kiểm tra riêng tại
  `/tmp/blockchain-firmware-esp32/build/`, giữ nguyên sdkconfig S3 cục bộ.
  Log trong `/tmp/blockchain-firmware-check/`.

## Nhập Wi-Fi bằng macro và mảng hai chiều

Sửa **`main/network_config.local.h`**, file đã tạo trong workspace này và
được Git ignore. Máy clone mới tạo file từ mẫu trước khi build:

```bash
cp main/network_config.example.h main/network_config.local.h
```

Ví dụ nội dung (thay SSID/mật khẩu bằng giá trị của bạn):

```c
#pragma once
#define HRC_WIFI_LIST_MODE 1
#define HRC_WIFI_NETWORKS { \
    {"WiFi_nha", "mat_khau_wifi_nha"}, \
    {"Hotspot",  "mat_khau_hotspot"}, \
}
#define HRC_MQTT_URI "mqtt://192.168.100.3:1885"
```

Firmware dùng macro để khai báo mảng thật:

```c
static const char *const s_known_wifi[][2] = HRC_WIFI_NETWORKS;
```

Cột 0 là SSID, cột 1 là password. Thêm/bớt dòng trong macro để đổi danh sách,
tối đa 16 dòng. SSID rỗng bị bỏ qua; password rỗng dành cho AP mở. SSID tối đa
32 byte, mật khẩu 8–63 ký tự hoặc PSK gồm 64 ký tự hex cho WPA2. Các dòng
không hợp lệ bị bỏ qua và báo số dòng, không log mật khẩu.

Khi boot, firmware quét và in SSID/RSSI/kênh, đối chiếu với mảng, chỉ thử AP
đã khai báo với kiểu bảo mật hỗ trợ. Chọn RSSI mạnh nhất; bằng RSSI thì ưu
tiên dòng trước. Kết nối lỗi/không lấy IP trong 15 s thì thử dòng tiếp theo.
Mỗi lượt đọc toàn bộ scan records, không chỉ 20 AP đầu. Mất kết nối hoặc hết
candidate thì quét lại với thời gian chờ 1/2/4/8/16/30 s; không đổi AP khi
đang nối ổn. SSID ẩn không được tự chọn từ kết quả scan.

Macro mode **không nạp Wi-Fi/broker cũ từ NVS** và không cần nhập lệnh serial.
`HRC_MQTT_URI` là IP LAN/cổng host của Docker; tại lần kiểm tra ngày
2026-10-02 là `192.168.100.3:1885`. Nếu đổi mạng, sửa macro rồi build/flash lại.
Sửa danh sách/password cũng cần build/flash; không cần erase NVS.

File mẫu commit được, local header chứa credentials không commit.
`main/network_config.h` nạp local header; nếu chưa có hoặc chọn
`HRC_WIFI_LIST_MODE=0`, firmware dùng đường Kconfig/NVS/serial bên dưới.

## Build và flash

Mở **ESP-IDF Terminal** trong VS Code với ESP-IDF 6.0.1 đã cài. Sau khi điền
local header, chọn cổng và target theo bo đang dùng. Ví dụ cho ESP32:

```bash
cd iot_code/esp32
HRC_PORT=/dev/ttyUSB0    # thay bằng cổng thực tế của bo
python -m esptool --port "$HRC_PORT" chip-id
idf.py set-target esp32
idf.py menuconfig       # kiểm tra GPIO và device_id; Wi-Fi nhập trong local header
idf.py build
idf.py -p "$HRC_PORT" flash monitor
```

Với ESP32-S3, thay bằng `idf.py set-target esp32s3` và port của bo đó, chẳng
hạn `/dev/ttyACM0`. Binary của hai target không dùng chung.
`sdkconfig.defaults` hiện mặc định S3 cho cấu hình ban đầu; lệnh `set-target`
chọn target tường minh. Nếu target đã đúng, các lần sau chỉ cần build/flash.

`set-target` tạo lại sdkconfig (bản trước thành sdkconfig.old), nên chạy
**trước** menuconfig. Hai file này và build output không commit vì có thể
chứa credentials. Flash app thông thường giữ NVS; macro mode bỏ qua mạng
đã lưu nên chỉ cần sửa local header, build và flash lại.

VS Code dùng flash type **UART**; chọn lại `idf.port` cho bo đang nối.
Cấu hình port lưu trong workspace là `/dev/ttyACM0`; cập nhật nếu bo đang dùng
xuất hiện ở cổng khác. Với SDK cài truyền thống, nạp
`. "$IDF_PATH/export.sh"`; ESP-IDF Terminal của Installation Manager đã cấp
môi trường, không cần tự ghép PATH hoặc sửa cấu hình global.

## Chế độ cũ: serial/NVS khi HRC_WIFI_LIST_MODE=0

Phần này chỉ áp dụng khi tắt macro list; app không khởi động serial setup
trong `HRC_WIFI_LIST_MODE=1`. Serial 115200 baud. Khi chưa có mạng hoặc kết
nối ban đầu thất bại, firmware
tự quét và in tối đa 20 mạng 2.4 GHz theo số, SSID, RSSI, kênh và kiểu bảo mật.
Gõ lệnh tại `hrc>`:

```text
wifi
join 1
<nhập mật khẩu khi được hỏi; chỉ hiện *>
broker mqtt://192.168.100.3:1885
status
```

IP trên là IP máy Docker tại lần kiểm tra ngày 2026-10-02; thay bằng IP LAN
hiện tại nếu mạng đổi. Docker của repo mở MQTT **1885**, API **8001**;
xem [docs/docker.md](../../docs/docker.md).

| Lệnh | Hành vi |
|---|---|
| `wifi` / `wifi scan` | Quét lại danh sách Wi-Fi |
| `join N` | Chọn dòng N; hỏi mật khẩu nếu mạng có bảo mật |
| `hidden` | Nhập SSID thủ công; mật khẩu rỗng cho mạng mở |
| `broker mqtt://HOST:PORT` | Đổi broker, restart MQTT và lưu NVS |
| `status` | Wi-Fi/IP, MQTT/broker, outbox, thời gian sync, GPIO và bộ đếm mẫu/lỗi/drop |
| `help` | Danh sách lệnh |

Backspace sửa ký tự; Esc/Ctrl-C hủy dòng nhập. Dòng quá dài bị từ chối,
không cắt âm thầm. Mật khẩu WPA2/WPA3 yêu cầu 8–63 ký tự hoặc PSK 64 hex;
SSID tối đa 32 byte. Mạng enterprise/legacy không hỗ trợ được đánh dấu.

Console đọc được UART và USB Serial/JTAG khi được chọn làm console chính
hoặc console phụ của S3. USB CDC dùng TinyUSB chưa có adapter trong bản này;
chọn UART hoặc USB Serial/JTAG trong menuconfig để bật `HRC_SERIAL_SETUP`.

NVS namespace `hrc_net` được ưu tiên trước Kconfig:

- Wi-Fi chỉ lưu sau khi lấy được IP; join thất bại giữ cấu hình đã lưu và
  khôi phục mạng trước đó. Nếu lưu NVS lỗi, console báo đã nối nhưng chưa lưu.
- Đổi broker lưu ngay khi URI hợp lệ; lưu thành công không có nghĩa broker
  đã reachable. Kiểm tra `status` và session ack.
- SSID rỗng khởi động radio để quét, không thử kết nối SSID rỗng. Reconnect
  Wi-Fi dùng backoff 1/2/4/8/16/30 s; MQTT chỉ start sau khi có IP.
- Sau khi lưu qua serial, đổi fallback menuconfig không ghi đè NVS;
  dùng `join`/`broker` để đổi cấu hình đang chạy.

## Kconfig và chu kỳ đo

Menu **HRC Safety Log Firmware**:

| Option | Mặc định | Ý nghĩa |
|---|---|---|
| `HRC_SERIAL_SETUP` | y với UART/USB Serial-JTAG | Console mạng cho chế độ cũ |
| `HRC_WIFI_SSID`, `HRC_WIFI_PASS` | rỗng | Chỉ dùng ở chế độ cũ khi NVS chưa có mạng |
| `CONFIG_HRC_MQTT_URI` | `mqtt://192.168.1.10:1883` | Fallback Kconfig ở chế độ cũ; macro mode dùng HRC_MQTT_URI local header |
| `HRC_DEVICE_ID` | `esp32_01` | Khớp `HRC_DEVICE_ID` của Compose |
| `HRC_SAMPLE_PERIOD_MS` | 100 | Chu kỳ đo và enqueue |
| `HRC_DISTANCE_LOG_PERIOD_MS` | 1000 | Khoảng cách giữa hai log số đo; 100 để log từng mẫu, 0 để tắt |
| `HRC_TRIG_GPIO`, `HRC_ECHO_GPIO` | 4, 5 | Chọn chân theo schematic bo |
| `HRC_ECHO_TIMEOUT_MS` | 30 | Timeout; echo_us schema tối đa 30000 µs |
| `HRC_SOUND_SPEED_M_S` | 343 | distance_mm = echo_us × v / 2000 |

SNTP có ba server khi `CONFIG_LWIP_SNTP_MAX_SERVERS=3`. Chưa sync:
`measured_at=null`, `time_synced=false`. Không thay bằng giờ backend.
Network/console khởi động trước sensor; sensor init lỗi vẫn cho cấu hình
mạng, nhưng không tạo task đo. Task đo dùng `xTaskDelayUntil`, bỏ deadline
đã lỡ để tránh phát nhiều mẫu dồn sau khi bị chặn.

## Log khoảng cách và gửi MQTT

Firmware in số đo mỗi **1000 ms**, kể cả khi MQTT chưa nối hoặc mailbox đầy:

```text
I (...) telemetry: distance_mm=171 echo_us=1000 valid=1 error=null sequence=21
```

Đây là ví dụ định dạng. Mẫu lỗi in `distance_mm=null`, `valid=0` và mã lỗi;
không thay bằng số đo giả. Đổi **Distance log interval** trong
`idf.py menuconfig` → **HRC Safety Log Firmware**; đặt 100 ms để xem từng
mẫu ở chu kỳ đo mặc định hoặc 0 để tắt. Log có nhịp riêng, không đổi chu kỳ đo.

Telemetry và session ack được copy vào mailbox RAM với thời gian chờ 0:
16 mẫu telemetry và 4 ack. Task `mqtt_tx` riêng xử lý API của esp-mqtt;
socket timeout hoặc chờ mutex MQTT không chặn task đo. `ESP_OK` tại enqueue
nghĩa là đã nhận vào mailbox, chưa khẳng định broker đã nhận. Log ack cũng
phân biệt `ack queued` với PUBACK thực tế.

Task gửi ưu tiên subscribe control và ack. Telemetry chờ SUBACK cấp QoS1;
subscribe lỗi thử lại sau 1 s, thiếu SUBACK thử lại sau 15 s. Outbox SDK giới
hạn **32768 byte**; telemetry dùng tối đa **30720 byte**, chừa 2048 byte cho
control/ack, và ack vẫn chừa 256 byte cho subscribe. Kiểm tra dung lượng tính
cả header/topic/packet ID. Khi chạm ngưỡng, giữ mẫu trong RAM và chờ PUBACK;
resume khi outbox xuống dưới nửa ngân sách telemetry để tránh bật/tắt liên tục.

Nếu còn gói đã enqueue nhưng không có PUBACK mới trong 15 s, worker yêu cầu
disconnect để esp-mqtt reconnect. Keepalive 20 s, reconnect 5 s, retransmit
5 s và network timeout 10 s. Trong chế độ serial cũ, `status` có `control`,
`pending`, `outbox` và tổng `pubacks`; outbox là snapshot từ worker, đọc
snapshot không chờ mutex mạng.

Mailbox và outbox đều hữu hạn, không lưu offline bền vững trên flash. Mất
mạng lâu/mailbox đầy vẫn mất mẫu mới và tăng `enqueue_dropped`; mẫu quá hạn
trong SDK có thể bị xóa. `pending` phân biệt backlog RAM với `outbox` của SDK.
Các kiểm tra native xác minh retry/ưu tiên/copy payload/không gọi SDK trong
task đo; vẫn cần flash để kiểm tra socket/radio thực tế trên bo.

## Chẩn đoán lỗi trong log

Lỗi sensor/enqueue lặp lại được tổng hợp mỗi 5 s; chuyển lỗi và hồi phục vẫn
báo ngay. `status` giữ tổng số mẫu invalid và enqueue drop. Receiver được
reset khi không có recv_done, lỗi TRIG hoặc phiên RMT không khởi động được.
Mẫu lỗi vẫn invalid/UNKNOWN; firmware không thay bằng khoảng cách giả.

- `ECHO_STUCK_LOW`: không nhận được cạnh lên sau TRIG. Kiểm tra đúng GPIO
  4/5, nguồn HC-SR04, GND chung và đường Echo qua mạch chia áp.
- `ECHO_STUCK_HIGH`: Echo đã cao trước khi đo.
- `ECHO_TIMEOUT`: có cạnh lên nhưng không xuống đúng hạn; không dùng xung bị
  RMT clamp làm số đo. Nếu echo hoàn tất nhưng distance >4500 mm, telemetry
  giữ echo_us thật, distance_mm=null và vẫn báo ECHO_TIMEOUT.
- `TRIG_FAIL`: lỗi điều khiển TRIG, start/reset RMT. Driver thử phục hồi để
  chu kỳ sau có thể đo tiếp.
- MQTT `Host is unreachable`: đặt đúng IP LAN/cổng 1885 trong macro
  `HRC_MQTT_URI`, build/flash lại và kiểm tra container broker.
- `telemetry enqueue loss: ESP_ERR_NO_MEM`: mailbox RAM đã đầy. Xem `control`,
  `pending`, `outbox` và tiến độ `pubacks`. Worker giữ message chưa enqueue
  thành công, chừa dung lượng control và chủ động reconnect nếu PUBACK ngừng.
  Log mất mẫu mới và tổng drop vẫn giữ để nhận biết khoảng trống dữ liệu.

ECHO HC-SR04 cần hạ mức điện áp trước khi vào GPIO ESP32; đấu nối trong
[docs/wiring.md](../../docs/wiring.md). Log Echo thấp và lỗi monitor USB
không đủ để khẳng định nguyên nhân phần cứng đã được sửa bằng firmware.

## Module và payload

`network_setup.c` xử lý lệnh/input, `network_console.c` nối UART/USB,
`network_config.h` nạp macro/mảng Wi-Fi, `wifi_mqtt.c` quản lý
scan/select/retry/radio/NVS/SNTP/MQTT, `diagnostics.c` điều tiết log,
`hcsr04.c` đo RMT, `telemetry.c` giữ sequence/control và build JSON.
`app_main.c` khởi động các module; `main/idf_component.yml` resolve MQTT cho
IDF >=6.

Telemetry giữ đúng 11 field schema v1, khóa `(device_id,boot_id,sequence)`.
Boot ID 16 hex mới mỗi boot, sequence từ 1; monotonic_us chốt trước TRIG.
Control chỉ chấp nhận `session_start`/`session_stop` và session_id hợp lệ;
callback đẩy vào mailbox 4 lệnh, task đo xử lý rồi ack. Lệnh lạ/thiếu ID và
mailbox đầy không được ack. ACK khi chưa đo mẫu nào dùng sequence=1 theo
schema; session lúc đó bắt đầu từ mẫu 2.
