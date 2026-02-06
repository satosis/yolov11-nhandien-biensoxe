# **Nhận diện biển số xe với YOLO**
- [Nhận diện biển số xe với YOLO](#nhận-diện-biển-số-xe-với-YOLO)
  - [Giới thiệu](#giới-thiệu)
  - [Tổng quan](#tổng-quan)
  - [Model](#model)
  - [Các phần phụ thuộc](#các-phần-phụ-thuộc)
  - [Cài đặt project](#cài-đặt-project)
  - [Thông tin](#thông-tin)


## **Tổng quan**

  Nội dung:

    1. Tìm hiểu về biển số xe và hệ thống nhận diện biển số xe.
    2. Phát biểu bài toán và hướng giải quyết.
    3. Nghiên cứu một số thuật toán xử lý ảnh và nhận diện kí tự ứng dụng trong việc nhận diện biển số xe.

  Từ nội dung nêu trên, đề tài sẽ bao gồm các nhiệm vụ sau:

    1. Tìm hiểu khái quát về xử lý ảnh và bài toán nhận diện biển số xe.
    2. Tìm hiểu các công đoạn chính của bài toán nhận diện biển số xe gồm 3 khâu chính:
      - Phát hiện vị trí và tách biển số xe.
      - Cắt vùng chứa kí tự.
      - Nhận diện kí tự.
    3. Cài đặt thử nghiệm.

## **Model**

  Model phát hiện biển số xe được sử dụng để phát hiện các biển số xe. Mô hình đã được huấn luyện bằng YOLO trong **50** epoch với **10125** hình ảnh có kích thước `480x480`.

  Mô hình đã được huấn luyện có sẵn trong [./models](./models/license_plate_recognition.pt).

  dataset được sử dụng để train model trong project này có sẵn tại [Roboflow Universe](https://universe.roboflow.com/roboflow-universe-projects/license-plate-recognition-rxg4e/dataset/11).

  Bạn có thể tự train model bằng Google Colab với các dataset khác tại [Roboflow Universe](https://universe.roboflow.com/) hoặc các nguồn khác.

  Source code train model hiện hành có trong file `colab_train.txt`. Ngoài ra có source code train model nâng cao hơn trong `colab_train_enhance.txt`.

## **Các phần phụ thuộc**

- Python 3.x (khuyến nghị Python 3.8-3.12)
- opencv_contrib_python 4.11.0.86
- opencv_python 4.11.0.86
- Ultralytics 8.3.160
- EasyOCR 1.7.2
- python-dotenv (Quản lý biến môi trường)
- requests (Gửi thông báo Telegram/n8n)

## **Hệ thống Cửa Cuốn Thông Minh**

Dự án đã được nâng cấp với các tính năng:
1. **Nhận diện đa đối tượng**: Xe tải, Người, Khuôn mặt và Biển số xe.
2. **Logic đếm thông minh**: 
   - Tự động đếm xe tải/người vào/ra.
   - Quy tắc đặc biệt: Xe tải ra khỏi cửa mặc định trừ -1 người bên phải xe.
3. **Cảnh báo an toàn**: Thông báo Telegram nếu cửa mở > 5 phút mà không có người.
4. **Nhật ký sự kiện**: Lưu toàn bộ lịch sử vào database SQLite (`door_events.db`).
5. **Điều khiển Telegram**: Sử dụng lệnh `/stats` để xem thống kê nhanh.

## **Cấu hình Project**

1. Tạo và cấu hình file [**.env**]:
2. Cài đặt các phần phụ thuộc mới:
   ```bash
   pip install -r requirements.txt
   ```

## **Cài đặt n8n trên Orange Pi**

Bạn có thể sử dụng script có sẵn để cài đặt n8n:

1. Cấp quyền thực thi cho script:
   ```bash
   chmod +x n8n/n8n.sh
   ```
2. Chạy script cài đặt:
   ```bash
   sudo ./n8n/n8n.sh install
   ```
3. Sau khi cài đặt, bạn có thể quản lý n8n bằng menu:
   ```bash
   ./n8n/n8n.sh
   ```