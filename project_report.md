# Báo Cáo Tổng Hợp Dự Án
## Hệ Thống Kiểm Soát Cửa Cuốn Thông Minh & Nhận Diện Biển Số

### 1. Giới Thiệu
Dự án nhằm xây dựng một hệ thống kiểm soát an ninh tự động cho cửa cuốn nhà kho/gara, sử dụng công nghệ AI để nhận diện biển số xe và khuôn mặt, tích hợp với Smart Home (Home Assistant) và điều khiển qua Telegram.

### 2. Kiến Trúc Hệ Thống
Hệ thống hoạt động trên một bo mạch nhúng **Orange Pi 4 Pro** (hoặc tương đương), xử lý trung tâm cho mọi tác vụ:

- **Input**:
  - Camera IP (RTSP) giám sát cửa.
  - Cảm biến/Trạng thái từ Home Assistant (Tuya).
- **Processing (Core)**:
  - **YOLOv8 (models/yolo26n.pt)**: Phát hiện người, xe tải, xe con.
  - **PaddleOCR**: Đọc biển số xe Việt Nam (2 dòng/1 dòng).
  - **Face Recognition**: Nhận diện khuôn mặt người.
  - **Logic**: So khớp Whitelist, kiểm tra trạng thái cửa.
- **Output**:
  - **Điều khiển Cửa**: Qua Home Assistant (Tuya Integration).
  - **Thông báo**: Qua Telegram (Gửi tin nhắn + Ảnh).
  - **Live View**: MJPEG Stream qua Web/Home Assistant.

### 3. Tính Năng Chính
#### A. Kiểm Soát Ra Vào (Access Control)
- **Tự động Mở cửa**: Khi phát hiện **Biển số Xe** nằm trong danh sách **Nhân viên** (`Staff`).
- **Cảnh báo Người lạ**:
  - Khi phát hiện khuôn mặt không có trong dữ liệu -> Gửi cảnh báo Telegram kèm ảnh crop.
  - Có thể duyệt người lạ thành người quen ngay trên Telegram bằng lệnh.
- **Cảnh báo Xe lạ**:
  - Khi phát hiện biển số không có trong dữ liệu -> Gửi cảnh báo Telegram.
  - Có thể duyệt xe lạ thành xe nhân viên hoặc từ chối.

#### B. An Ninh & An Toàn
- **Cảnh báo Cửa mở**:
  - Nếu cửa mở quá 5 phút mà không có người -> Gửi cảnh báo.
  - Nếu mất tín hiệu Camera quá 30 giây -> Gửi cảnh báo.
- **Trạng thái Cửa**: Theo dõi trạng thái (Mở/Đóng) thời gian thực thông qua xử lý hình ảnh (AI) hoặc cảm biến Tuya.

#### C. Tương Tác Người Dùng
- **Telegram Bot 2 chiều**:
  - Nhận cảnh báo (ảnh + text).
  - Gửi lệnh điều khiển:
    - `/open`: Mở cửa ngay lập tức.
    - `/staff [BIEN_SO]`: Thêm xe vào whitelist.
    - `/staff_face [ID] [TEN]`: Thêm người vào whitelist.
    - `/reject`: Bỏ qua cảnh báo.
- **Live Stream**: Xem trực tiếp video có lớp phủ (bounding box, tên) qua trình duyệt hoặc Home Assistant.

### 4. Phần Cứng & Phần Mềm
#### Phần cứng
1.  **Orange Pi 4 Pro** (Server AI).
2.  **Camera IP** (Imou/Dahua/Hikvision) hỗ trợ RTSP.
3.  **Công tắc Cửa cuốn Tuya** (Wifi).

#### Phần mềm
- **Ngôn ngữ**: Python 3.10+.
- **AI Core**: Ultralytics YOLOv8, PaddleOCR, Face_recognition (Dlib).
- **Backend**: FastAPI (MJPEG Streamer), Paho-MQTT.
- **Integration**: Home Assistant (MQTT Broker + Tuya Integration).

### 5. Quy Trình Hoạt Động (Workflow)
1.  **Khởi động**: `main.py` chạy 3 luồng (Xử lý ảnh, API Server, Telegram Polling).
2.  **Vòng lặp Xử lý**:
    - Đọc frame từ Camera.
    - YOLO phát hiện Xe/Người.
    - Nếu là Xe -> Crop biển số -> OCR -> Kiểm tra DB.
      - *Quen*: Gửi lệnh MQTT mở cửa.
      - *Lạ*: Gửi Telegram báo xe lạ.
    - Nếu là Người -> Crop mặt -> So khớp.
      - *Quen*: Hiển thị tên.
      - *Lạ*: Gửi Telegram báo người lạ kèm ID tạm.
3.  **Phản hồi từ Người dùng**:
    - Người dùng chat lệnh `/staff` hoặc `/staff_face` trên Telegram.
    - Hệ thống cập nhật DB/File cấu hình và reload model ngay lập tức.

### 6. Cài Đặt & Vận Hành
- **Cài đặt**: Chạy `install.sh` để cài môi trường.
- **Cấu hình**: Chỉnh sửa file `.env` (RTSP URL, Telegram Token...).
- **Chạy**: `python main.py`.
- **Giám sát**: Truy cập `http://IP:8000/video_feed`.
