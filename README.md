# **Nhận diện biển số xe với YOLO11**
- [Nhận diện biển số xe với YOLO11](#nhận-diện-biển-số-xe-với-YOLO11)
  - [Giới thiệu](#giới-thiệu)
  - [Tổng quan](#tổng-quan)
  - [Model](#model)
  - [Các phần phụ thuộc](#các-phần-phụ-thuộc)
  - [Cài đặt project](#cài-đặt-project)
  - [Thông tin](#thông-tin)

## **Giới thiệu**

  **HỌC PHẦN TRÍ TUỆ NHÂN TẠO (A.I)**

  **Nhóm 3 - Lớp IT2K59 - Trường Đại học Giao thông Vận tải**

  *Thành viên:*

  ```
  Nguyễn Văn Tuấn

  Hoàng Trọng Nguyên

  Hoàng Quốc Phương

  Nguyễn Mạnh Hùng

  Phạm Quang Minh
  ```

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

  Model phát hiện biển số xe được sử dụng để phát hiện các biển số xe. Mô hình đã được huấn luyện bằng YOLO11 trong **50** epoch với **10125** hình ảnh có kích thước `480x480`.

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

## **Cài đặt project**

Trong project này, EasyOCR sẽ nhận diện biển số xe thông qua webcam.

Mọi câu lệnh dưới đây đã được chúng mình thử nghiệm với Python 3.12.10 và PyCharm 2025.1.2. Bạn cũng có thể sử dụng IDE Python khác ngoài PyCharm.

Nếu sử dụng phiên bản Python cũ hơn và không thực thi được các câu lệnh này, bạn có thể update lên phiên bản giống như trên hoặc tìm các câu lệnh phù hợp từ các nguồn bên ngoài.

Bạn có thể cấu hình trình thông dịch Python (Python Interpreter) và PyCharm sẽ cho phép bạn tạo môi trường ảo (Virtualenv) và file môi trường ảo sẽ được lưu trong thư mục `.venv`.

- Mặc định môi trường ảo .venv sẽ được kích hoạt, nếu không bạn có thể chạy lệnh sau:

  ```bash
  .venv/Scripts/activate
  ```

- Cài đặt các phần phụ thuộc:

  ```bash
  pip install -r requirements.txt
  ```

Lưu ý: Đường dẫn đến file `requirements.txt` không được chứa khoảng trắng/tiếng Việt có dấu.

- Bạn có thể cập nhật pip lên phiên bản mới nhất (nếu cần) với câu lệnh:

  ```bash
  python.exe -m pip install --upgrade pip
  ```

- Cài đặt PyTorch:

  - Gỡ cài đặt bản cũ:

    ```bash
    pip3 uninstall torch torchvision torchaudio
    ```

  - Nếu bạn muốn chạy trên CPU hoặc không có GPU:

    ```bash
    pip3 install torch torchvision torchaudio
    ```

    Tại dòng 8 `ocrReader = easyocr.Reader(['en','vi'], gpu=True)`, đổi `gpu=True` thành `gpu=False`.

  - Nếu bạn muốn chạy trên GPU:

    Kiểm tra phiên bản CUDA trong cmd với câu lệnh sau: `nvidia-smi`

    - CUDA 11.8

      ```bash
      pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
      ```

    - CUDA 12.6

      ```bash
      pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu126
      ```

    - CUDA 12.8

      ```bash
      pip3 install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
      ```

    Nếu muốn cài PyTorch hỗ trợ đúng phiên bản CUDA của mình bằng cách thử thay 3 số cuối thành phiên bản của bạn.

    Ví dụ: CUDA 12.7:  thay `cu128` thành `cu127`. Hiện tại PyTorch hỗ trợ cài đến 12.8. Bạn có thể kiểm tra tại trang chủ của [PyTorch](https://pytorch.org/).

- Chạy chương trình:

  ```bash
  python main.py
  ```

- Sau khi làm việc với môi trường ảo xong, bạn có thể tắt môi trường ảo bằng câu lệnh:

  ```bash
  deactivate
  ```

## **Thông tin**

Mã nguồn được phát triển bởi Nhóm 3 (A.I) lớp IT2K59 – Trường Đại học Giao thông Vận tải.
