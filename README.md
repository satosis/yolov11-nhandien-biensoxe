# Hệ Thống Nhận Diện Biển Số & Camera AI (Orange Pi)

Chạy toàn bộ hệ thống bằng một lệnh:

```bash
chmod +x install.sh
./install.sh
```

Sau khi cài đặt xong, chạy ứng dụng:
```bash
source venv/bin/activate
python main.py
```

## Mô hình AI
Hệ thống sử dụng model `models/bien_so_xe.pt` cho cả nhận diện vật thể và biển số, tối ưu hóa cho tốc độ trên thiết bị biên.

## Cấu hình Bảo mật
Sửa file `.env` (được tạo từ `.env.example` sau khi chạy install):

```
TELEGRAM_BOT_TOKEN=
CHAT_ID_IMPORTANT=
CHAT_ID_REGULAR=
RTSP_URL=
OCR_SOURCE=
```

## Cấu hình Camera RTSP
Sửa trong `.env`:
- `CAMERA_MAC`: MAC cố định của camera (khuyến nghị dùng để tự động tìm IP).
- `CAMERA_IP_SUBNET`: subnet nội bộ để quét khi cần (ví dụ `10.115.215.0/24`).
- `CAMERA_IP`: không cần khai báo thủ công trong `.env` (sẽ tự tạo runtime từ `CAMERA_MAC`).
- `RTSP_URL`: Đường dẫn luồng hình ảnh chính từ Camera (khuyến nghị `@{CAMERA_IP}`; nếu còn để IP cũ, app sẽ tự thay host theo `CAMERA_IP` runtime).
- `OCR_SOURCE`: Nguồn nhận diện (vd: `rtsp` hoặc `webcam`).

Trong `deploy/frigate/config.yml`, địa chỉ stream dùng biến `{CAMERA_IP}`.
Khi chạy `./cmd up`, script `deploy/scripts/resolve_camera_ip.py` sẽ tự dò `CAMERA_IP` theo `CAMERA_MAC` và ghi vào `.camera.env` trước khi khởi động Docker.

## Tính năng Đếm Người & Xe
- Hệ thống tự động đếm số lượng người và xe tải ra/vào.
- Logic "Gate Logic": Tự động trừ số người khi có xe đi ra (tài xế).
- Các tham số tinh chỉnh trong `.env`:
  - `LEFT_EXIT_WINDOW_SECONDS`: Thời gian chờ cửa sổ thoát.
  - `MAX_ACTIVE_VEHICLE_EXIT_SESSIONS`: Số phiên xe hoạt động tối đa.

## Lệnh Telegram (Quản lý)
Sử dụng trong nhóm chat:
- Bot sẽ tự đăng ký danh sách lệnh Telegram (menu `/`) khi `event_bridge` khởi động.
- `/gate_closed`: Đặt trạng thái cửa là ĐÓNG.
- `/gate_open`: Đặt trạng thái cửa là MỞ.
- `/gate_status`: Xem trạng thái cửa + số người/xe.
- `/report`: Xem báo cáo nhanh.
- `/start` hoặc `/help`: Hiển thị menu lệnh nhanh.

## Báo cáo Tháng
- Báo cáo dạng văn bản:
  ```bash
  ./cmd report-month YYYY-MM
  ```
- Biểu đồ (PNG lưu tại `./data/event_bridge/reports/`):
  ```bash
  ./cmd chart-month YYYY-MM
  ```

## Home Assistant (Tích hợp Nhà thông minh)
Truy cập: `http://192.168.1.131:8123`

Tích hợp sẵn:
- **Tuya Integration**: Điều khiển cửa cuốn tự động.
- **MQTT Discovery**: Tự động nhận diện cảm biến.
  - `sensor.shed_people_count`: Đếm người.
  - `sensor.shed_vehicle_count`: Đếm xe.
  - `cover.garage_door`: Điều khiển cửa cuốn.

### Tự động hóa (Automation)
- Tự động mở cửa Tuya khi nhận diện biển số xe quen (`whitelist`).
- Tự động đóng cửa sau 5 phút nếu không có người.

## Điều khiển PTZ (Camera xoay 360)
Cấu hình trong `.env` để Home Assistant điều khiển xoay camera:
- `ONVIF_HOST`, `ONVIF_PORT`, `ONVIF_USER`, `ONVIF_PASS`
- `ONVIF_PRESET_GATE`: Vị trí soi cổng.
- `ONVIF_PRESET_PANORAMA`: Vị trí toàn cảnh.

Hành vi:
- Khi chuyển sang toàn cảnh, OCR tạm dừng.
- Tự động quay về vị trí cổng sau `PTZ_AUTO_RETURN_SECONDS` giây.

## Các lệnh vận hành (trong thư mục dự án)
```bash
./cmd stats         # Xem thống kê
./cmd today         # Xem sự kiện hôm nay
./cmd last 50       # 50 sự kiện gần nhất
./cmd pending       # Danh sách biển số lạ chờ duyệt
./cmd whitelist     # Danh sách biển số quen
./cmd gate          # Trạng thái cổng
./cmd test-ptz      # Test tính năng xoay camera
```

## Xử lý sự cố (Troubleshooting)
- **Lỗi RTSP**: Kiểm tra đường dẫn, user/pass camera trong `.env`.
- **Lỗi MQTT**: Kiểm tra container `mosquitto` hoặc Log.
- **Lỗi export ONNX (`No module named onnxscript`)**: chạy lại `source venv/bin/activate && pip install -r requirements.txt` để cài `onnx` + `onnxscript`, rồi chạy lại export model.
- **Cảnh báo ONNX opset/version converter**: script export mặc định dùng `opset=18` để tránh lỗi convert từ opset thấp (ví dụ lỗi `No Adapter To Version ... for Resize`). Có thể chạy tay: `python3 deploy/utils/export_model.py models/bien_so_xe.pt onnx 18`.
- **Cảnh báo `onnxruntime ... GPU device discovery failed` trên Orange Pi CPU-only**: đây là cảnh báo phụ khi optimize/simplify ONNX, không làm export fail. Script hiện mặc định `simplify=False` cho ONNX để giảm cảnh báo này; nếu cần tối ưu thêm thì bật `ONNX_SIMPLIFY=1` trước khi export.
- **Python version**: dự án đang chạy tốt với Python 3.10.x (ví dụ `Python 3.10.12`).
- **Lỗi `IndentationError` trong `core/config.py`**: chạy `python3 -m py_compile core/config.py`; installer sẽ tự thử `git checkout -- core/config.py` và fallback template. Nếu vẫn lỗi, chạy `git pull` rồi thử lại.
- **Lỗi `Cannot resolve CAMERA_IP from CAMERA_MAC`**: script hiện sẽ tự quét nhiều dải mạng LAN (bao gồm interface nội bộ và fallback), nhưng bạn vẫn nên đặt `CAMERA_IP_SUBNET` đúng dải mạng (vd `10.115.215.0/24`) để dò nhanh/chính xác hơn, rồi chạy lại `./cmd up`.
- **Lỗi `env file .camera.env not found` khi `./cmd up`**: đã được xử lý trong lệnh `./cmd up` mới (tự tạo `.camera.env` rỗng trước khi chạy Docker). Nếu đang dùng bản cũ, cập nhật mã mới hoặc tự tạo tạm bằng `touch .camera.env`.
- **Lỗi Cửa cuốn**: Kiểm tra kết nối Tuya trong Home Assistant hoặc file `core/door_controller.py`.
