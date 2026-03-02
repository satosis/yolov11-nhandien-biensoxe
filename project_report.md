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
  - **YOLOv8 (models/bien_so_xe.pt)**: Phát hiện người, xe tải, xe con.
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

### 7. Tiền xử lý biển số và tách ký tự (cải thiện OCR)
#### 7.1 Xoay biển số để chuẩn hóa góc nhìn
Trong thực tế, ảnh biển số thường bị nghiêng trái/phải hoặc méo phối cảnh. Nếu đưa trực tiếp vùng crop vào OCR, độ chính xác giảm đáng kể và dễ nhầm lẫn các cặp ký tự tương tự như `1/7`, `2/Z`, `8/B`.

Quy trình xoay biển số được áp dụng:
1. Xác định 2 đỉnh nằm dưới cùng của biển số là `A(x1, y1)` và `B(x2, y2)`.
2. Tính góc nghiêng từ chênh lệch tọa độ hai điểm (cạnh đối/cạnh kề của tam giác vuông suy ra từ A-B).
3. Xoay ảnh theo góc đã tính để cạnh đáy biển số song song trục ngang.
4. Nếu `A` cao hơn `B` thì dùng góc âm, ngược lại dùng góc dương.

Kết quả là ảnh biển số sau xoay có bố cục ký tự ổn định hơn, giúp bước tìm contour ký tự phía sau hoạt động tốt hơn.

#### 7.2 Tìm vùng ký tự và lọc nhiễu
Từ ảnh nhị phân của biển số, hệ thống trích xuất các contour và loại bỏ các contour nhiễu (viền biển số, dấu chấm, vạch nhỏ, ký tự giả) dựa trên nhóm điều kiện hình học:
- Diện tích contour tối thiểu/tối đa.
- Tỷ lệ rộng/cao của bounding box.
- Vị trí tương đối theo trục dọc/ngang so với vùng biển số.
- Mật độ/độ đặc điểm ảnh trong contour.

Các contour thỏa điều kiện sẽ được biểu diễn bằng hình chữ nhật bao quanh ký tự ứng viên.

#### 7.3 Tách ký tự và đưa vào bộ nhận diện
Sau khi có các bounding box ký tự:
1. Sắp xếp theo thứ tự đọc (trái → phải, trên → dưới nếu biển 2 dòng).
2. Cắt từng ký tự từ **ảnh nhị phân** (không cắt từ ảnh gốc màu).
3. Resize/chuẩn hóa kích thước đầu vào cho mô hình OCR hoặc bộ phân loại ký tự.
4. Nhận diện ký tự và ghép chuỗi biển số cuối cùng.

Cách làm này giúp pipeline OCR ổn định hơn trong điều kiện ánh sáng yếu, góc chụp lệch và nền nhiễu.
