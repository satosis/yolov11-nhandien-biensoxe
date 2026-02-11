# Hướng Dẫn Cài Đặt Thiết Bị

Tài liệu này hướng dẫn chi tiết từng bước cài đặt và cấu hình các thiết bị trong hệ thống nhận diện biển số và điều khiển cửa cuốn.

---

## 1. Thiết bị Tuya Smart (Cửa cuốn)

### Bước 1: Thêm thiết bị vào App Tuya
1.  Tải App **Tuya Smart** trên điện thoại.
2.  Đăng ký tài khoản và đăng nhập.
3.  Cấp nguồn cho bộ điều khiển cửa cuốn (Switch).
4.  Trên App, nhấn dấu **(+)** -> **Add Device**.
  - Chọn **Wi-Fi Switch**. Đưa thiết bị về chế độ pairing (đèn nháy nhanh).
5.  Làm theo hướng dẫn trên màn hình để kết nối Wifi và hoàn tất.
6.  Thử điều khiển Mở/Đóng trên App để đảm bảo hoạt động.

### Bước 2: Tích hợp vào Home Assistant
1.  Mở Home Assistant (truy cập `http://192.168.1.131:8123`).
2.  Vào **Settings** (Cài đặt) -> **Devices & Services** (Thiết bị & Dịch vụ).
3.  Nhấn nút **Add Integration** (Thêm tích hợp) ở góc dưới.
4.  Tìm kiếm từ khóa: **"Tuya"**.
5.  Chọn **Tuya** -> Sẽ hiện ra mã QR.
6.  Mở App Tuya trên điện thoại -> Tab **Me** (Tôi) -> Biểu tượng quét QR ở góc trên -> Quét mã trên màn hình Home Assistant.
7.  Xác nhận cấp quyền. Các thiết bị sẽ tự động xuất hiện.
8.  Ghi lại **Entity ID** của cửa cuốn (Ví dụ: `cover.garage_door_switch_1`).

---

## 2. Camera Imou A32 3MP (Ranger 2)

### Bước 1: Cấu hình mạng
1.  Kết nối Camera vào Wifi nhà bạn bằng App **Imou Life**.
2.  Vào phần cài đặt Wifi của Router (hoặc dùng App Advanced IP Scanner) để tìm **IP của Camera** (Ví dụ: `192.168.1.55`).
    *   *Khuyên dùng: Đặt IP Tĩnh (Static IP) cho Camera trong cài đặt Router để tránh bị đổi IP khi mất điện.*

### Bước 2: Lấy Safety Code (Mật khẩu RTSP)
1.  Lật đáy camera lên.
2.  Tìm dòng chữ **Safety Code** (thường là chuỗi ký tự viết hoa, ví dụ: `L2F4A...`). Đây chính là mật khẩu RTSP.

### Bước 3: Cấu hình vào hệ thống
Sửa file `.env` trên Orange Pi bằng lệnh `nano .env`:

```bash
# Cấu hình RTSP cho Imou
# admin: mặc định
# SAFETY_CODE: Mã dưới đáy cam
# IP_CAMERA: Địa chỉ IP camera (VD: 192.168.1.55)
RTSP_URL="rtsp://admin:SAFETY_CODE@192.168.1.55:554/cam/realmonitor?channel=1&subtype=0"
```

> **Cách test link:** Dùng phần mềm **VLC** trên máy tính -> Media -> Open Network Stream -> Dán link trên vào. Nếu xem được hình là thành công.

---

## 3. Orange Pi (Server)

### Bước 1: Cài đặt hệ điều hành
1.  Tải **Ubuntu Server** cho Orange Pi 4 Pro (từ trang chủ Orange Pi hoặc Armbian).
2.  Dùng **Balena Etcher** để flash file ảnh (`.img`) vào thẻ nhớ MicroSD (tối thiểu 32GB, Class 10).
3.  Cắm thẻ nhớ, dây mạng, nguồn điện vào Orange Pi.
4.  Tìm IP của Orange Pi trên Router (hoặc dùng phần mềm *Advanced IP Scanner*).

### Bước 2: Kết nối SSH
Dùng Terminal (Mac/Linux) hoặc PuTTY/PowerShell (Windows):
```bash
ssh root@192.168.1.131
# Mật khẩu mặc định thường là 1234 hoặc orangepi
```

### Bước 3: Cài đặt Project
Sau khi SSH vào:
```bash
# Cài git nếu chưa có
apt update && apt install git -y

# Clone code về
git clone https://github.com/your-repo/yolov11-nhandien-biensoxe
cd yolov11-nhandien-biensoxe

# Chạy lệnh cài đặt tự động
chmod +x install.sh
./install.sh
```

---

## 4. Cấu hình Tự động hóa (Automation)

Sau khi cài xong, bạn cần liên kết Camera với Cửa Tuya.

1.  Vào thư mục project trên Orange Pi.
2.  Mở file `deploy/homeassistant/automations.yaml`.
3.  Tìm đoạn:
    ```yaml
    action:
      - service: cover.open_cover
        target:
          entity_id: cover.garage_door  # <--- SỬA CÁI NÀY
    ```
4.  Thay `cover.garage_door` bằng Entity ID bạn lấy ở **Mục 1 - Bước 2**.
5.  Lưu file.
6.  Trên giao diện Home Assistant: Vào **Developer Tools** -> Nhấn **Restart** để áp dụng.

---

## 5. Kiểm tra toàn hệ thống

1.  Chạy chương trình chính:
    ```bash
    source venv/bin/activate
    python main.py
    ```
2.  Đưa một biển số xe "Quen" (đã add vào whitelist) ra trước Camera.
3.  Màn hình sẽ hiện: `✅ Xe quen: 29A-123.45 -> MỞ CỬA`.
4.  Cửa cuốn của bạn sẽ tự động mở.
